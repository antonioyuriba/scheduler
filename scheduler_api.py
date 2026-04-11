import json
import os
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_iso_to_utc(value: str) -> datetime:
    """
    Converte ISO-8601 (com Z, com offset, ou naive) para aware UTC.
    Naive é interpretado como UTC (política explícita).
    """
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class MessageStatus:
    """
    Estado explícito da máquina de estados de uma mensagem.
    Transições válidas:
      scheduled  → processing (início do disparo)
      processing → sent       (2xx: seguido de delete)
      processing → failed     (erro: enfileira retry)
      failed     → processing (sweep retry)
      *          → dead_letter (via _move_to_dlq)
    O sweep só olha para scheduled e failed; nunca toca em processing.
    """
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


def _new_version() -> str:
    return uuid.uuid4().hex

import redis
import redis.exceptions as redis_exceptions
import requests
import schedule
from fastapi import FastAPI, HTTPException, Depends, Header, Query, Body
from pydantic import BaseModel
import uvicorn
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Scheduler API", version="2.4.0")

API_TOKEN = os.getenv('API_TOKEN')

# ==========================================
# CONFIGURAÇÃO DE ROTA INTERNA DO N8N
# ==========================================
# N8N_INTERNAL_URL é OPCIONAL. Default vazio = sem rewrite, usa sempre a URL
# pública do webhook (mais seguro: zero dependência de IP ou nome de serviço
# Docker). Defina apenas se quiser otimizar rede interna via nome de serviço
# estável (ex.: "http://n8n:5678"). NUNCA use IP Docker (172.x.x.x) — muda
# quando o Coolify recria containers e derruba a aplicação.
N8N_EXTERNAL_HOST = os.getenv('N8N_EXTERNAL_HOST', 'n8n-prod.byiatech.com.br')
N8N_INTERNAL_URL = os.getenv('N8N_INTERNAL_URL', '').strip()

# Retry config
WEBHOOK_MAX_RETRIES = int(os.getenv('WEBHOOK_MAX_RETRIES', 3))
WEBHOOK_RETRY_DELAY = int(os.getenv('WEBHOOK_RETRY_DELAY', 10))
WEBHOOK_TIMEOUT = int(os.getenv('WEBHOOK_TIMEOUT', 30))

# Intervalo para varredura de mensagens atrasadas (segundos)
SWEEP_INTERVAL = int(os.getenv('SWEEP_INTERVAL', 60))

# Se True, mensagens vencidas na subida são ignoradas (não disparam, ficam no Redis)
SKIP_OVERDUE_ON_STARTUP = os.getenv('SKIP_OVERDUE_ON_STARTUP', 'false').lower() == 'true'

# Mensagens mais antigas que este limite (em horas) são descartadas e removidas do Redis
# Aceita valores fracionários (ex.: 0.5 = 30min). Use 0 para desabilitar o limite.
MESSAGE_MAX_AGE_HOURS = float(os.getenv('MESSAGE_MAX_AGE_HOURS', '24'))

# Tolerância (segundos) para aceitar scheduleTo levemente no passado sem
# rejeitar com 400. Acima disso, POST /messages retorna 400.
PAST_SCHEDULE_TOLERANCE_SECONDS = int(os.getenv('PAST_SCHEDULE_TOLERANCE_SECONDS', '30'))

# Dead Letter Queue: mensagens mortas (DLQ) ficam em chaves dead:message:{id}:{version}
# com TTL em dias. Índice secundário dead:index:{id} tem todas as versões mortas.
DLQ_TTL_DAYS = int(os.getenv('DLQ_TTL_DAYS', '30'))

# Quantas falhas de webhook até a mensagem virar DLQ em vez de continuar sendo retentada.
MAX_FAIL_COUNT = int(os.getenv('MAX_FAIL_COUNT', '10'))

# Janela máxima que uma mensagem pode ficar em retry depois do primeiro disparo
# (_firstAttemptAt). Acima disso, vira DLQ com reason "retry_deadline".
RETRY_DEADLINE_HOURS = float(os.getenv('RETRY_DEADLINE_HOURS', '6'))

# Máximo de webhooks disparando em paralelo (evita travar o servidor)
WEBHOOK_MAX_CONCURRENT = int(os.getenv('WEBHOOK_MAX_CONCURRENT', 5))
webhook_semaphore = threading.Semaphore(WEBHOOK_MAX_CONCURRENT)


def _worst_case_fire_seconds() -> int:
    """Tempo máximo que um fire_webhook pode levar, somando timeouts e backoff."""
    total = 0
    for attempt in range(1, WEBHOOK_MAX_RETRIES + 1):
        total += WEBHOOK_TIMEOUT
        if attempt < WEBHOOK_MAX_RETRIES:
            total += WEBHOOK_RETRY_DELAY * attempt
    return total + 30  # buffer


# Claim distribuído por mensagem (evita disparo duplo em overlap de deploy):
# - INFLIGHT_TTL_SECONDS é o TTL do lock no Redis; derivado do pior caso de retry.
# - INFLIGHT_HEARTBEAT_SECONDS é o intervalo com que o worker renova o lease
#   enquanto está processando. Sempre < TTL / 4 para margem ampla.
INFLIGHT_TTL_SECONDS = int(os.getenv('INFLIGHT_TTL_SECONDS', str(_worst_case_fire_seconds())))
INFLIGHT_HEARTBEAT_SECONDS = max(5, INFLIGHT_TTL_SECONDS // 4)

# Identidade deste processo — aparece no valor do lock para debug/auditoria.
CONTAINER_ID = f"{os.uname().nodename}:{uuid.uuid4().hex[:8]}"


def log(msg: str):
    print(f"[{now_utc().isoformat()}] {msg}")


def _is_too_old(schedule_timestamp: str) -> bool:
    if MESSAGE_MAX_AGE_HOURS <= 0:
        return False
    try:
        schedule_time = parse_iso_to_utc(schedule_timestamp)
        age = now_utc() - schedule_time
        return age.total_seconds() > MESSAGE_MAX_AGE_HOURS * 3600
    except Exception:
        return False


def verify_token(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")
    token = authorization.replace("Bearer ", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return token


redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', 'localhost'),
    port=int(os.getenv('REDIS_PORT', 6379)),
    password=os.getenv('REDIS_PASSWORD'),
    decode_responses=True,
    socket_connect_timeout=10,
    socket_timeout=10,
    retry_on_timeout=True,
)


class ScheduleMessage(BaseModel):
    id: str
    scheduleTo: str
    payload: Dict[str, Any]
    webhookUrl: str


class BulkDeleteFilters(BaseModel):
    prefix: Optional[str] = None
    contains: Optional[str] = None


scheduled_jobs: Dict[str, schedule.Job] = {}
schedule_lock = threading.RLock()


def update_message_cas(
    message_id: str,
    expected_version: str,
    mutator: Callable[[Dict[str, Any]], Dict[str, Any]],
    max_retries: int = 3,
) -> Optional[Dict[str, Any]]:
    """
    Read-modify-write atômico usando WATCH + MULTI/EXEC do Redis.

    - Lê a mensagem; se _version não bate com expected_version (ou se a
      chave sumiu), retorna None sem gravar. O chamador interpreta None
      como "a mensagem foi substituída ou removida — abortar".
    - Aplica mutator sobre uma cópia do dict, atribui novo _version, e
      grava de volta. Cada mutação gera uma versão nova.
    - Em WatchError (alguém escreveu entre o WATCH e o EXEC), retry até
      max_retries.
    - Esta função é o único caminho permitido para qualquer RMW (exceto
      a criação inicial no POST e a migração inline no restore). Escritas
      cegas fora dessa disciplina são proibidas na Fase 2.
    """
    key = f"message:{message_id}"
    for _attempt in range(max_retries):
        try:
            with redis_client.pipeline() as pipe:
                pipe.watch(key)
                raw = pipe.get(key)
                if not raw:
                    pipe.unwatch()
                    return None
                try:
                    data = json.loads(raw)
                except Exception:
                    pipe.unwatch()
                    return None
                if data.get("_version") != expected_version:
                    pipe.unwatch()
                    return None
                new_data = mutator(dict(data))
                new_data["_version"] = _new_version()
                pipe.multi()
                pipe.set(key, json.dumps(new_data))
                pipe.execute()
                return new_data
        except redis_exceptions.WatchError:
            continue
        except Exception as e:
            log(f"[CAS] update failed on {message_id}: {e}")
            return None
    log(f"[CAS] update exhausted retries for {message_id}")
    return None


def _safe_delete_cas(message_id: str, expected_version: str) -> bool:
    """
    DELETE condicional: só remove a chave se a _version atual bate.
    Retorna True se deletou (ou se já não existia), False se a versão
    divergiu (alguém substituiu a mensagem enquanto disparávamos).
    """
    key = f"message:{message_id}"
    for _attempt in range(3):
        try:
            with redis_client.pipeline() as pipe:
                pipe.watch(key)
                raw = pipe.get(key)
                if not raw:
                    pipe.unwatch()
                    return True
                try:
                    data = json.loads(raw)
                except Exception:
                    pipe.unwatch()
                    return False
                if data.get("_version") != expected_version:
                    pipe.unwatch()
                    return False
                pipe.multi()
                pipe.delete(key)
                pipe.execute()
                return True
        except redis_exceptions.WatchError:
            continue
        except Exception as e:
            log(f"[CAS-DEL] failed on {message_id}: {e}")
            return False
    return False


_RENEW_CLAIM_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""

_RELEASE_CLAIM_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def _try_claim(message_id: str) -> Optional[str]:
    """
    Tenta adquirir o claim para uma mensagem via SET NX EX. Retorna o token
    (valor gravado) se conseguiu, ou None se outro processo já detém o claim.
    Usado para evitar disparo duplo em overlap de deploy (container antigo
    drenando enquanto o novo sobe) ou em multi-replica futura.
    """
    token = f"{CONTAINER_ID}:{now_utc().isoformat()}"
    try:
        ok = redis_client.set(
            f"lock:message:{message_id}",
            token,
            nx=True,
            ex=INFLIGHT_TTL_SECONDS,
        )
    except Exception as e:
        log(f"[CLAIM] try failed for {message_id}: {e}")
        return None
    return token if ok else None


def _renew_claim(message_id: str, token: str) -> bool:
    """Renova o TTL se o token ainda é nosso (atômico via Lua)."""
    try:
        return bool(redis_client.eval(
            _RENEW_CLAIM_LUA,
            1,
            f"lock:message:{message_id}",
            token,
            INFLIGHT_TTL_SECONDS,
        ))
    except Exception:
        return False


def _release_claim(message_id: str, token: str) -> None:
    """Libera apenas se o token ainda é nosso (atômico via Lua)."""
    try:
        redis_client.eval(
            _RELEASE_CLAIM_LUA,
            1,
            f"lock:message:{message_id}",
            token,
        )
    except Exception:
        pass


def _move_to_dlq(
    message_id: str,
    reason: str,
    extra: Optional[Dict[str, Any]] = None,
    expected_version: Optional[str] = None,
) -> bool:
    """
    Move uma mensagem para a DLQ usando WATCH + MULTI/EXEC no Redis.

    - Chave destino: dead:message:{id}:{version}. A versão vem de _version se
      já existir; caso contrário, usa "v0" (mensagens antigas pré-versionamento).
    - Índice dead:index:{id} (Set) guarda todas as versões mortas daquele id,
      útil para histórico de auditoria.
    - Se expected_version for passado, só move se a versão atual bate (CAS).
      Na Fase 1 os callers passam None (ainda não há versionamento).
    - Nunca dispara mensagem antiga: esta é a saída "limpa" para mensagens
      perdidas (missed_window, retry_deadline, max_retries, too_old).
    - Aplica TTL de DLQ_TTL_DAYS na chave e no índice para evitar crescimento
      infinito.
    """
    src = f"message:{message_id}"
    for _attempt in range(3):
        try:
            with redis_client.pipeline() as pipe:
                pipe.watch(src)
                raw = pipe.get(src)
                if not raw:
                    pipe.unwatch()
                    return False
                try:
                    data = json.loads(raw)
                except Exception:
                    pipe.unwatch()
                    return False

                current_version = data.get("_version")
                if expected_version is not None and current_version != expected_version:
                    pipe.unwatch()
                    return False

                version_for_key = current_version or "v0"
                dst = f"dead:message:{message_id}:{version_for_key}"
                index_key = f"dead:index:{message_id}"

                data["_deadReason"] = reason
                data["_deadAt"] = now_utc().isoformat()
                data["status"] = "dead_letter"
                if extra:
                    data.update(extra)

                ttl_seconds = DLQ_TTL_DAYS * 86400
                pipe.multi()
                pipe.set(dst, json.dumps(data), ex=ttl_seconds)
                pipe.sadd(index_key, version_for_key)
                pipe.expire(index_key, ttl_seconds)
                pipe.delete(src)
                pipe.execute()

                log(f"[DLQ] {message_id}@{version_for_key} -> dead-letter (reason={reason})")
                return True
        except redis_exceptions.WatchError:
            continue
        except Exception as e:
            log(f"[DLQ] Failed to move {message_id}: {e}")
            return False
    log(f"[DLQ] Gave up moving {message_id} after WATCH retries")
    return False


def _rewrite_webhook_url(url: str) -> str:
    if N8N_EXTERNAL_HOST and N8N_INTERNAL_URL and N8N_EXTERNAL_HOST in url:
        parts = url.split(N8N_EXTERNAL_HOST, 1)
        if len(parts) == 2:
            return f"{N8N_INTERNAL_URL}{parts[1]}"
    return url


def _fire_webhook_post(webhook_url: str, payload: Dict[str, Any]) -> requests.Response:
    """
    Dispara o POST do webhook. Se N8N_INTERNAL_URL estiver configurado e
    conseguir reescrever a URL, tenta o endereço interno primeiro. Em caso de
    ConnectionError/Timeout (n8n interno indisponível), faz fallback para a
    URL pública original. 4xx/5xx NÃO acionam fallback — são resposta legítima
    do endpoint e devem subir para a lógica de retry.
    """
    internal_url = _rewrite_webhook_url(webhook_url)
    if internal_url == webhook_url:
        response = requests.post(webhook_url, json=payload, timeout=WEBHOOK_TIMEOUT)
        response.raise_for_status()
        return response

    try:
        response = requests.post(internal_url, json=payload, timeout=WEBHOOK_TIMEOUT)
        response.raise_for_status()
        return response
    except (requests.ConnectionError, requests.Timeout) as e:
        log(f"Internal URL {internal_url} unreachable ({type(e).__name__}); falling back to public {webhook_url}")
        response = requests.post(webhook_url, json=payload, timeout=WEBHOOK_TIMEOUT)
        response.raise_for_status()
        return response


def _build_next_run_map() -> Dict[str, Optional[str]]:
    next_run_map: Dict[str, Optional[str]] = {}
    with schedule_lock:
        for job in list(schedule.jobs):
            try:
                tag_id = list(job.tags)[0] if job.tags else None
                if tag_id:
                    next_run_map[tag_id] = job.next_run.isoformat() if job.next_run else None
            except Exception:
                pass
    return next_run_map


def _iter_message_keys_by_filter(prefix: Optional[str] = None, contains: Optional[str] = None):
    if not prefix and not contains:
        raise HTTPException(status_code=400, detail="Informe ao menos um filtro: 'prefix' ou 'contains'.")

    if prefix:
        for key in redis_client.scan_iter(match=f"message:{prefix}*", count=1000):
            yield key
        return

    for key in redis_client.scan_iter(match="message:*", count=1000):
        try:
            msg_id = key.split("message:", 1)[1]
            if contains and (contains in msg_id):
                yield key
        except Exception:
            pass


def fire_webhook(message_id: str, webhook_url: str, payload: Dict[str, Any], expected_version: str):
    """
    Entry point do disparo. Fluxo:
      1. Espera slot do webhook_semaphore (limita paralelismo).
      2. Tenta claim distribuído (SET NX). Se falha, outro processo já está
         disparando essa mensagem — skip silencioso.
      3. Inicia thread de heartbeat que renova o lease periodicamente.
      4. Delega para _fire_webhook_inner, que faz CAS + POST + CAS.
      5. Sempre no finally: libera heartbeat e claim.
    """
    with webhook_semaphore:
        token = _try_claim(message_id)
        if not token:
            log(f"[{message_id}] claim unavailable — another instance is processing; skipping")
            return

        stop_hb = threading.Event()

        def _hb():
            while not stop_hb.wait(INFLIGHT_HEARTBEAT_SECONDS):
                if not _renew_claim(message_id, token):
                    log(f"[{message_id}] lost claim during execution (token expired or stolen)")
                    stop_hb.set()
                    return

        hb_thread = threading.Thread(target=_hb, daemon=True, name=f"hb-{message_id[:16]}")
        hb_thread.start()

        try:
            _fire_webhook_inner(message_id, webhook_url, payload, expected_version)
        finally:
            stop_hb.set()
            _release_claim(message_id, token)


def _clear_scheduled_memory(message_id: str) -> None:
    with schedule_lock:
        scheduled_jobs.pop(message_id, None)
        try:
            schedule.clear(message_id)
        except Exception:
            pass


def _fire_webhook_inner(
    message_id: str,
    webhook_url: str,
    payload: Dict[str, Any],
    expected_version: str,
):
    internal_url_preview = _rewrite_webhook_url(webhook_url)
    if internal_url_preview != webhook_url:
        log(f"URL rewrite enabled for {message_id}: {webhook_url} -> {internal_url_preview} (with fallback)")

    # Transição scheduled/failed → processing via CAS. Grava _firstAttemptAt
    # (o único carimbo que autoriza retries). Se CAS falha, a mensagem foi
    # substituída ou removida — ABORTAR. Disparar a versão antiga seria
    # exatamente o race que estamos tentando prevenir.
    def _mark_processing(d: Dict[str, Any]) -> Dict[str, Any]:
        d["status"] = MessageStatus.PROCESSING
        if not d.get("_firstAttemptAt"):
            d["_firstAttemptAt"] = now_utc().isoformat()
        d["_lastAttempt"] = now_utc().isoformat()
        return d

    updated = update_message_cas(message_id, expected_version, _mark_processing)
    if updated is None:
        log(f"[{message_id}] CAS mismatch before fire (expected v={expected_version[:8]}); aborting — message was replaced or removed")
        _clear_scheduled_memory(message_id)
        return
    current_version = updated["_version"]

    last_error = None
    for attempt in range(1, WEBHOOK_MAX_RETRIES + 1):
        try:
            _fire_webhook_post(webhook_url, payload)
            log(f"Webhook fired successfully for message {message_id} (attempt {attempt})")

            if _safe_delete_cas(message_id, current_version):
                log(f"Message {message_id} cleaned from Redis")
            else:
                log(f"Message {message_id} CAS mismatch on delete — newer version exists, leaving it")

            _clear_scheduled_memory(message_id)
            return

        except requests.exceptions.Timeout:
            last_error = f"Timeout (attempt {attempt}/{WEBHOOK_MAX_RETRIES})"
            log(f"Webhook timeout for {message_id}: {last_error}")
        except requests.exceptions.ConnectionError as e:
            last_error = f"ConnectionError (attempt {attempt}/{WEBHOOK_MAX_RETRIES}): {e}"
            log(f"Webhook connection error for {message_id}: {last_error}")
        except requests.exceptions.HTTPError as e:
            last_error = f"HTTP {e.response.status_code} (attempt {attempt}/{WEBHOOK_MAX_RETRIES})"
            log(f"Webhook HTTP error for {message_id}: {last_error}")
            if e.response.status_code < 500:
                log(f"Client error {e.response.status_code} for {message_id}, skipping retries")
                break
        except Exception as e:
            last_error = f"Unexpected error (attempt {attempt}/{WEBHOOK_MAX_RETRIES}): {e}"
            log(f"Webhook unexpected error for {message_id}: {last_error}")

        if attempt < WEBHOOK_MAX_RETRIES:
            delay = WEBHOOK_RETRY_DELAY * attempt
            log(f"Retrying {message_id} in {delay}s...")
            time.sleep(delay)

    log(f"ALL {WEBHOOK_MAX_RETRIES} attempts FAILED for {message_id}. Last error: {last_error}")

    # Transição processing → failed via CAS. Se falha, a mensagem foi
    # substituída/removida no meio do disparo — parar silenciosamente.
    def _mark_failed(d: Dict[str, Any]) -> Dict[str, Any]:
        d["_failCount"] = d.get("_failCount", 0) + 1
        d["_lastError"] = str(last_error)
        d["_lastFailure"] = now_utc().isoformat()
        d["status"] = MessageStatus.FAILED
        return d

    result = update_message_cas(message_id, current_version, _mark_failed)
    if result is None:
        log(f"[{message_id}] CAS mismatch on mark_failed — message replaced/removed, stopping silently")
    else:
        log(f"Message {message_id} marked as failed (failCount={result.get('_failCount')}) — sweep will retry")

    _clear_scheduled_memory(message_id)


def schedule_message(
    message_id: str,
    schedule_timestamp: str,
    webhook_url: str,
    payload: Dict[str, Any],
    version: str,
):
    """
    Agenda uma mensagem. A versão capturada aqui é a que o fire_webhook vai
    usar como expected_version no CAS. Se algum POST substituir a mensagem
    no meio do caminho, o disparo detecta a versão nova e aborta.
    """
    with schedule_lock:
        if message_id in scheduled_jobs:
            schedule.clear(message_id)
            scheduled_jobs.pop(message_id, None)

        schedule_time_utc = parse_iso_to_utc(schedule_timestamp)
        now = now_utc()

        if schedule_time_utc <= now:
            # Política absoluta: mensagem no passado nunca dispara. Vai para DLQ
            # com reason "missed_window". O POST /messages já rejeita
            # explicitamente no passado; este ramo cobre chamadas internas
            # (restore/sweep) que ainda caem aqui.
            log(f"Message {message_id} scheduleTo in the past ({schedule_timestamp}); moving to DLQ")
            _move_to_dlq(message_id, "missed_window")
            return

        # A lib `schedule` usa datetime.now() local naive para run_pending().
        # Convertemos para local naive APENAS aqui para preencher next_run.
        # Requer TZ=UTC no container (fixado no Dockerfile).
        local_dt = schedule_time_utc.astimezone().replace(tzinfo=None)

        captured_version = version

        def job():
            t = threading.Thread(
                target=fire_webhook,
                args=(message_id, webhook_url, payload, captured_version),
                daemon=True
            )
            t.start()
            return schedule.CancelJob

        job_instance = schedule.every().day.at(local_dt.strftime("%H:%M:%S")).do(job).tag(message_id)
        job_instance.next_run = local_dt

        scheduled_jobs[message_id] = job_instance
        log(f"Message {message_id} scheduled for {schedule_time_utc.isoformat()} UTC (v={version[:8]})")


def scheduler_worker():
    while True:
        try:
            with schedule_lock:
                schedule.run_pending()
        except Exception as e:
            log(f"Error in scheduler_worker: {e}")
        time.sleep(1)


def sweep_failed_messages():
    while True:
        try:
            time.sleep(SWEEP_INTERVAL)
            now = now_utc()
            swept = 0

            for key in redis_client.scan_iter(match="message:*", count=1000):
                try:
                    raw = redis_client.get(key)
                    if not raw:
                        continue

                    data = json.loads(raw)
                    msg_id = data.get("id")
                    schedule_to = data.get("scheduleTo")
                    current_version = data.get("_version")

                    if not msg_id or not schedule_to:
                        continue

                    # Sem _version? Mensagem legacy que o restore ainda não
                    # migrou; pular e deixar para o próximo sweep.
                    if not current_version:
                        continue

                    # Nunca tocar em mensagens em disparo ativo.
                    if data.get("status") == MessageStatus.PROCESSING:
                        continue

                    schedule_time_utc = parse_iso_to_utc(schedule_to)

                    if schedule_time_utc > now:
                        # Futuro: garantir que está agendado em memória.
                        with schedule_lock:
                            if msg_id not in scheduled_jobs:
                                log(f"[SWEEP] Re-scheduling future message {msg_id} (scheduleTo: {schedule_to})")
                                schedule_message(msg_id, schedule_to, data["webhookUrl"], data["payload"], current_version)
                        continue

                    # ========================================================
                    # Mensagem no passado. Política absoluta:
                    # - Sem _firstAttemptAt → nunca disparou → missed_window → DLQ
                    # - Com _firstAttemptAt  → retry legítimo, sujeito a limites
                    # ========================================================
                    first_attempt = data.get("_firstAttemptAt")
                    if not first_attempt:
                        log(f"[SWEEP] {msg_id} overdue without _firstAttemptAt; moving to DLQ (missed_window)")
                        _move_to_dlq(msg_id, "missed_window_sweep")
                        continue

                    # Retry deadline: muito tempo tentando desde o primeiro disparo.
                    try:
                        first_dt = parse_iso_to_utc(first_attempt)
                        if (now - first_dt).total_seconds() > RETRY_DEADLINE_HOURS * 3600:
                            log(f"[SWEEP] {msg_id} exceeded RETRY_DEADLINE_HOURS; moving to DLQ")
                            _move_to_dlq(
                                msg_id,
                                "retry_deadline",
                                {"_failCount": data.get("_failCount", 0)},
                            )
                            continue
                    except Exception:
                        pass

                    # Idade absoluta da mensagem.
                    if _is_too_old(schedule_to):
                        log(f"[SWEEP] {msg_id} older than MESSAGE_MAX_AGE_HOURS; moving to DLQ")
                        _move_to_dlq(
                            msg_id,
                            "too_old",
                            {"_ageLimitHours": MESSAGE_MAX_AGE_HOURS},
                        )
                        continue

                    fail_count = data.get("_failCount", 0)
                    if fail_count >= MAX_FAIL_COUNT:
                        log(f"[SWEEP] {msg_id} hit MAX_FAIL_COUNT={MAX_FAIL_COUNT}; moving to DLQ")
                        _move_to_dlq(
                            msg_id,
                            "max_retries",
                            {"_failCount": fail_count},
                        )
                        continue

                    with schedule_lock:
                        has_job = msg_id in scheduled_jobs

                    if not has_job:
                        last_failure = data.get("_lastFailure")
                        if last_failure:
                            try:
                                last_fail_time = parse_iso_to_utc(last_failure)
                                if (now - last_fail_time).total_seconds() < 300:
                                    continue
                            except Exception:
                                pass

                        log(f"[SWEEP] Retrying overdue message {msg_id} (failCount: {fail_count}, v={current_version[:8]})")
                        swept += 1
                        t = threading.Thread(
                            target=fire_webhook,
                            args=(msg_id, data["webhookUrl"], data["payload"], current_version),
                            daemon=True
                        )
                        t.start()

                except Exception as e:
                    log(f"[SWEEP] Error processing {key}: {e}")

            if swept > 0:
                log(f"[SWEEP] Fired {swept} overdue messages")

        except Exception as e:
            log(f"[SWEEP] Error in sweep loop: {e}")


def restore_scheduled_messages():
    try:
        restored_count = 0
        migrated_count = 0
        dlq_count = 0
        deferred_to_sweep = 0
        now = now_utc()

        for key in redis_client.scan_iter(match="message:*", count=1000):
            try:
                raw = redis_client.get(key)
                if not raw:
                    continue
                data = json.loads(raw)
                msg_id = data.get("id")
                schedule_to = data.get("scheduleTo", "")

                if not msg_id or not schedule_to:
                    continue

                try:
                    schedule_time_utc = parse_iso_to_utc(schedule_to)
                except Exception:
                    log(f"[STARTUP] {msg_id} has invalid scheduleTo; moving to DLQ")
                    _move_to_dlq(msg_id, "invalid_scheduleTo_on_startup")
                    dlq_count += 1
                    continue

                # Migração inline: mensagens pré-PR 3 não têm _version nem status.
                # Atribuímos no primeiro read e reescrevemos (blind set — restore
                # é single-thread, antes dos workers começarem a mexer em chaves).
                if not data.get("_version"):
                    data["_version"] = _new_version()
                    data["status"] = data.get("status") or MessageStatus.SCHEDULED
                    try:
                        redis_client.set(key, json.dumps(data))
                        migrated_count += 1
                        log(f"[STARTUP] migrated legacy message {msg_id} (v={data['_version'][:8]})")
                    except Exception as e:
                        log(f"[STARTUP] {msg_id} failed migration, skipping: {e}")
                        continue

                version = data["_version"]

                if schedule_time_utc <= now:
                    # Política absoluta:
                    # - Sem _firstAttemptAt → missed_window → DLQ
                    # - Com _firstAttemptAt → retry legítimo → delegar ao sweep
                    first_attempt = data.get("_firstAttemptAt")
                    if not first_attempt:
                        log(f"[STARTUP] {msg_id} overdue without _firstAttemptAt; moving to DLQ")
                        _move_to_dlq(msg_id, "missed_window_on_startup")
                        dlq_count += 1
                        continue
                    log(f"[STARTUP] {msg_id} overdue with _firstAttemptAt; deferring to sweep")
                    deferred_to_sweep += 1
                    continue

                # Futuro: reagendar normalmente.
                schedule_message(msg_id, schedule_to, data["webhookUrl"], data["payload"], version)
                restored_count += 1
                log(f"Restored scheduled message - ID: {msg_id}")
            except Exception as e:
                log(f"Failed to restore message {key}: {e}")

        log(
            f"Restore summary: restored={restored_count} migrated={migrated_count} "
            f"dlq={dlq_count} deferred_to_sweep={deferred_to_sweep}"
        )
    except Exception as e:
        log(f"Error restoring messages: {e}")


@app.on_event("startup")
def _startup():
    # Roda restore em thread separada para não bloquear o event loop do FastAPI
    t0 = threading.Thread(target=restore_scheduled_messages, daemon=True)
    t0.start()

    t1 = threading.Thread(target=scheduler_worker, daemon=True)
    t1.start()

    t2 = threading.Thread(target=sweep_failed_messages, daemon=True)
    t2.start()

    log("Scheduler API started with retry support and sweep worker")


# =======================
# ROTAS
# =======================

@app.post("/messages")
async def create_scheduled_message(message: ScheduleMessage, token: str = Depends(verify_token)):
    try:
        try:
            schedule_time_utc = parse_iso_to_utc(message.scheduleTo)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid scheduleTo format (expected ISO-8601): {e}"
            )

        threshold = now_utc() - timedelta(seconds=PAST_SCHEDULE_TOLERANCE_SECONDS)
        if schedule_time_utc < threshold:
            age_seconds = (now_utc() - schedule_time_utc).total_seconds()
            log(f"Rejecting past scheduleTo - ID: {message.id} ({age_seconds:.0f}s in the past)")
            raise HTTPException(
                status_code=400,
                detail=(
                    f"scheduleTo is in the past ({message.scheduleTo}); "
                    f"refusing to schedule. Tolerance: {PAST_SCHEDULE_TOLERANCE_SECONDS}s."
                )
            )

        redis_key = f"message:{message.id}"

        if redis_client.exists(redis_key):
            log(f"Message exists, updating - ID: {message.id}")
        else:
            log(f"Creating new message - ID: {message.id}")

        version = _new_version()
        message_data = {
            "id": message.id,
            "scheduleTo": message.scheduleTo,
            "payload": message.payload,
            "webhookUrl": message.webhookUrl,
            "_version": version,
            "status": MessageStatus.SCHEDULED,
            "_createdAt": now_utc().isoformat(),
        }

        redis_client.set(redis_key, json.dumps(message_data))
        log(f"Message stored in Redis - ID: {message.id} (v={version[:8]})")

        schedule_message(message.id, message.scheduleTo, message.webhookUrl, message.payload, version)

        return {"status": "scheduled", "messageId": message.id, "version": version}

    except HTTPException:
        raise
    except Exception as e:
        log(f"Error in create: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to schedule message: {str(e)}")


@app.get("/messages")
async def list_scheduled_messages(token: str = Depends(verify_token)):
    try:
        with schedule_lock:
            jobs_snapshot = list(schedule.jobs)

        jobs = []
        for job in jobs_snapshot:
            jobs.append({
                "messageId": list(job.tags)[0] if job.tags else "unknown",
                "nextRun": job.next_run.isoformat() if job.next_run else None,
                "job": str(job.job_func)
            })

        return {"scheduledJobs": jobs, "count": len(jobs)}

    except Exception as e:
        log(f"Error listing jobs: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list jobs: {str(e)}")


@app.get("/messages/search")
async def search_messages(
    prefix: Optional[str] = Query(default=None),
    contains: Optional[str] = Query(default=None),
    token: str = Depends(verify_token),
):
    try:
        next_run_map = _build_next_run_map()
        results = []

        for key in _iter_message_keys_by_filter(prefix=prefix, contains=contains):
            raw = redis_client.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
                msg_id = data.get("id") or key.split("message:", 1)[1]
                results.append({
                    "id": msg_id,
                    "scheduleTo": data.get("scheduleTo"),
                    "payload": data.get("payload"),
                    "webhookUrl": data.get("webhookUrl"),
                    "nextRun": next_run_map.get(msg_id),
                    "_failCount": data.get("_failCount"),
                    "_lastError": data.get("_lastError"),
                    "_lastFailure": data.get("_lastFailure"),
                })
            except Exception as e:
                log(f"Failed to parse message {key}: {e}")

        return {"count": len(results), "messages": results}
    except HTTPException:
        raise
    except Exception as e:
        log(f"Error in search_messages: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to search messages: {str(e)}")


@app.delete("/messages/bulk")
@app.delete("/messages/bulk/")
@app.post("/messages/bulk", include_in_schema=False)
@app.post("/messages/bulk/", include_in_schema=False)
async def bulk_delete_messages(
    prefix: Optional[str] = Query(default=None),
    contains: Optional[str] = Query(default=None),
    token: str = Depends(verify_token),
    body: Optional[BulkDeleteFilters] = Body(default=None),
):
    try:
        if not prefix and not contains and body:
            prefix, contains = body.prefix, body.contains

        if not prefix and not contains:
            raise HTTPException(status_code=400, detail="Informe ao menos um filtro: 'prefix' ou 'contains'.")

        deleted_ids = []
        for key in list(_iter_message_keys_by_filter(prefix=prefix, contains=contains)):
            try:
                message_id = key.split("message:", 1)[1]
            except Exception:
                continue

            redis_client.delete(key)

            with schedule_lock:
                try:
                    schedule.clear(message_id)
                except ValueError:
                    pass
                scheduled_jobs.pop(message_id, None)

            deleted_ids.append(message_id)

        return {"deleted": len(deleted_ids), "messageIds": deleted_ids}
    except HTTPException:
        raise
    except Exception as e:
        log(f"Error in bulk_delete_messages: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to bulk delete messages: {str(e)}")


@app.get("/messages/{message_id}")
async def get_scheduled_message(message_id: str, token: str = Depends(verify_token)):
    try:
        redis_key = f"message:{message_id}"
        message_data_json = redis_client.get(redis_key)

        if not message_data_json:
            raise HTTPException(status_code=404, detail=f"Message with ID '{message_id}' not found")

        return json.loads(message_data_json)

    except HTTPException:
        raise
    except Exception as e:
        log(f"Error in get_scheduled_message: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve message: {str(e)}")


@app.delete("/messages/{message_id}")
async def delete_scheduled_message(message_id: str, token: str = Depends(verify_token)):
    try:
        redis_key = f"message:{message_id}"
        redis_client.delete(redis_key)

        with schedule_lock:
            try:
                schedule.clear(message_id)
            except ValueError:
                log(f"No schedule found for ID: {message_id}")
            scheduled_jobs.pop(message_id, None)

        return {"status": "deleted", "messageId": message_id}

    except Exception as e:
        log(f"Error in delete: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete message: {str(e)}")


@app.get("/health")
async def health_check():
    health: Dict[str, Any] = {
        "status": "healthy",
        "redis": "unknown",
        "n8nInternal": "not_configured",
        "n8nExternal": "unknown",
        "version": "2.4.0",
        "containerId": CONTAINER_ID,
        "inflightTtlSeconds": INFLIGHT_TTL_SECONDS,
    }

    try:
        redis_client.ping()
        health["redis"] = "connected"
    except Exception as e:
        health["status"] = "unhealthy"
        health["redis"] = f"error: {type(e).__name__}"

    if N8N_INTERNAL_URL:
        try:
            r = requests.head(N8N_INTERNAL_URL, timeout=3, allow_redirects=False)
            health["n8nInternal"] = "reachable" if r.status_code < 500 else f"http_{r.status_code}"
        except Exception as e:
            health["n8nInternal"] = f"unreachable: {type(e).__name__}"
            if health["status"] == "healthy":
                health["status"] = "degraded"

    if N8N_EXTERNAL_HOST:
        try:
            r = requests.head(f"https://{N8N_EXTERNAL_HOST}", timeout=5, allow_redirects=False)
            health["n8nExternal"] = "reachable" if r.status_code < 500 else f"http_{r.status_code}"
        except Exception as e:
            health["n8nExternal"] = f"unreachable: {type(e).__name__}"
            if health["status"] == "healthy":
                health["status"] = "degraded"

    try:
        with schedule_lock:
            health["scheduledJobs"] = len(schedule.jobs)
    except Exception:
        health["scheduledJobs"] = None

    try:
        health["activeClaims"] = sum(1 for _ in redis_client.scan_iter(match="lock:message:*", count=1000))
    except Exception:
        health["activeClaims"] = None

    return health


@app.get("/stats")
async def stats(token: str = Depends(verify_token)):
    try:
        total_redis = 0
        failed_count = 0
        overdue_count = 0
        legacy_count = 0
        by_status: Dict[str, int] = {}
        now = now_utc()

        for key in redis_client.scan_iter(match="message:*", count=1000):
            total_redis += 1
            raw = redis_client.get(key)
            if raw:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if not data.get("_version"):
                    legacy_count += 1
                status_val = data.get("status") or "unknown"
                by_status[status_val] = by_status.get(status_val, 0) + 1
                if data.get("_failCount"):
                    failed_count += 1
                schedule_to = data.get("scheduleTo")
                if schedule_to:
                    try:
                        if parse_iso_to_utc(schedule_to) <= now:
                            overdue_count += 1
                    except Exception:
                        pass

        with schedule_lock:
            job_count = len(schedule.jobs)

        dead_count = sum(1 for _ in redis_client.scan_iter(match="dead:message:*", count=1000))

        return {
            "messagesInRedis": total_redis,
            "jobsInMemory": job_count,
            "failedMessages": failed_count,
            "overdueMessages": overdue_count,
            "deadLetterMessages": dead_count,
            "legacyMessages": legacy_count,
            "byStatus": by_status,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/dead")
async def list_dead_letter(
    limit: int = Query(default=100, ge=1, le=1000),
    token: str = Depends(verify_token),
):
    """
    Lista mensagens mortas (DLQ). Cada entrada é uma versão morta específica,
    identificada por {id, version}. A mesma mensagem pode aparecer múltiplas
    vezes se morreu mais de uma vez (p.ex. ID reutilizado).
    """
    try:
        items = []
        scanned = 0
        for key in redis_client.scan_iter(match="dead:message:*", count=1000):
            scanned += 1
            raw = redis_client.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            try:
                _, _, rest = key.split(":", 2)
                if ":" in rest:
                    msg_id, version = rest.rsplit(":", 1)
                else:
                    msg_id, version = rest, "v0"
            except Exception:
                msg_id, version = data.get("id", "unknown"), "v0"
            items.append({
                "id": msg_id,
                "version": version,
                "deadReason": data.get("_deadReason"),
                "deadAt": data.get("_deadAt"),
                "scheduleTo": data.get("scheduleTo"),
                "failCount": data.get("_failCount"),
                "lastError": data.get("_lastError"),
                "webhookUrl": data.get("webhookUrl"),
                "payload": data.get("payload"),
            })
            if len(items) >= limit:
                break

        items.sort(key=lambda it: it.get("deadAt") or "", reverse=True)
        return {"count": len(items), "truncated": len(items) >= limit, "items": items}
    except Exception as e:
        log(f"Error in list_dead_letter: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list DLQ: {str(e)}")


@app.get("/dead/{message_id}/versions")
async def list_dead_versions(message_id: str, token: str = Depends(verify_token)):
    """Lista todas as versões mortas de um mesmo id (via dead:index:{id})."""
    try:
        versions = redis_client.smembers(f"dead:index:{message_id}") or set()
        out = []
        stale = []
        for v in versions:
            raw = redis_client.get(f"dead:message:{message_id}:{v}")
            if not raw:
                stale.append(v)
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            out.append({
                "version": v,
                "deadReason": data.get("_deadReason"),
                "deadAt": data.get("_deadAt"),
                "failCount": data.get("_failCount"),
            })
        if stale:
            try:
                redis_client.srem(f"dead:index:{message_id}", *stale)
            except Exception:
                pass
        out.sort(key=lambda it: it.get("deadAt") or "", reverse=True)
        return {"id": message_id, "count": len(out), "versions": out}
    except Exception as e:
        log(f"Error in list_dead_versions: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list versions: {str(e)}")


if __name__ == "__main__":
    log("Starting Scheduler API server v2.4.0")
    uvicorn.run(app, host="0.0.0.0", port=8000)
