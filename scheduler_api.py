import json
import os
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import redis
import requests
import schedule
from fastapi import FastAPI, HTTPException, Depends, Header, Query, Body
from pydantic import BaseModel
import uvicorn
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Scheduler API", version="2.0.0")

API_TOKEN = os.getenv('API_TOKEN')

# ==========================================
# CONFIGURAÇÃO DE ROTA INTERNA DO N8N
# ==========================================
# Reescreve URLs externas do n8n para a rota interna Docker,
# evitando timeout por hairpin NAT (mesmo servidor).
N8N_EXTERNAL_HOST = os.getenv('N8N_EXTERNAL_HOST', 'n8n-prod.byiatech.com.br')
N8N_INTERNAL_URL = os.getenv('N8N_INTERNAL_URL', 'http://172.18.0.7:5678')

# Retry config
WEBHOOK_MAX_RETRIES = int(os.getenv('WEBHOOK_MAX_RETRIES', 3))
WEBHOOK_RETRY_DELAY = int(os.getenv('WEBHOOK_RETRY_DELAY', 10))  # segundos entre retries
WEBHOOK_TIMEOUT = int(os.getenv('WEBHOOK_TIMEOUT', 30))

# Intervalo para varredura de mensagens atrasadas (segundos)
SWEEP_INTERVAL = int(os.getenv('SWEEP_INTERVAL', 60))


def log(msg: str):
    """Log com timestamp padronizado."""
    print(f"[{datetime.utcnow().isoformat()}] {msg}")


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
    scheduleTo: str  # ISO 8601 (ex.: 2025-08-21T15:30:05-03:00)
    payload: Dict[str, Any]
    webhookUrl: str


class BulkDeleteFilters(BaseModel):
    prefix: Optional[str] = None
    contains: Optional[str] = None


# Controle de jobs em memória
scheduled_jobs: Dict[str, schedule.Job] = {}

# Lock para acesso thread-safe ao scheduler
schedule_lock = threading.RLock()


def _rewrite_webhook_url(url: str) -> str:
    """
    Reescreve URLs do n8n externo para a rota interna Docker.
    Ex: https://n8n-prod.byiatech.com.br/webhook/xxx
     -> http://172.18.0.7:5678/webhook/xxx
    """
    if N8N_EXTERNAL_HOST and N8N_INTERNAL_URL:
        if N8N_EXTERNAL_HOST in url:
            # Extrai o path após o host
            parts = url.split(N8N_EXTERNAL_HOST, 1)
            if len(parts) == 2:
                path = parts[1]  # /webhook/xxx
                new_url = f"{N8N_INTERNAL_URL}{path}"
                return new_url
    return url


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
        pattern = f"message:{prefix}*"
        for key in redis_client.scan_iter(match=pattern, count=1000):
            yield key
        return

    for key in redis_client.scan_iter(match="message:*", count=1000):
        try:
            msg_id = key.split("message:", 1)[1]
            if contains and (contains in msg_id):
                yield key
        except Exception:
            pass


def fire_webhook(message_id: str, webhook_url: str, payload: Dict[str, Any]):
    """
    Dispara o webhook com retry e backoff.
    SÓ limpa do Redis se o disparo for bem-sucedido.
    """
    # Reescreve URL para rota interna
    internal_url = _rewrite_webhook_url(webhook_url)
    if internal_url != webhook_url:
        log(f"URL rewritten for {message_id}: {webhook_url} -> {internal_url}")

    last_error = None
    for attempt in range(1, WEBHOOK_MAX_RETRIES + 1):
        try:
            response = requests.post(internal_url, json=payload, timeout=WEBHOOK_TIMEOUT)
            response.raise_for_status()
            log(f"Webhook fired successfully for message {message_id} (attempt {attempt})")

            # SUCESSO — agora sim limpa do Redis e da memória
            try:
                redis_client.delete(f"message:{message_id}")
                log(f"Message {message_id} cleaned from Redis")
            except Exception as redis_err:
                log(f"WARNING: Webhook fired but failed to clean Redis for {message_id}: {redis_err}")

            with schedule_lock:
                scheduled_jobs.pop(message_id, None)
                try:
                    schedule.clear(message_id)
                except Exception:
                    pass

            return  # Sucesso, sai da função

        except requests.exceptions.Timeout:
            last_error = f"Timeout (attempt {attempt}/{WEBHOOK_MAX_RETRIES})"
            log(f"Webhook timeout for {message_id}: {last_error}")
        except requests.exceptions.ConnectionError as e:
            last_error = f"ConnectionError (attempt {attempt}/{WEBHOOK_MAX_RETRIES}): {e}"
            log(f"Webhook connection error for {message_id}: {last_error}")
        except requests.exceptions.HTTPError as e:
            last_error = f"HTTP {e.response.status_code} (attempt {attempt}/{WEBHOOK_MAX_RETRIES})"
            log(f"Webhook HTTP error for {message_id}: {last_error}")
            # Se for 4xx (erro do cliente), não faz retry
            if e.response.status_code < 500:
                log(f"Client error {e.response.status_code} for {message_id}, skipping retries")
                break
        except Exception as e:
            last_error = f"Unexpected error (attempt {attempt}/{WEBHOOK_MAX_RETRIES}): {e}"
            log(f"Webhook unexpected error for {message_id}: {last_error}")

        # Espera antes do próximo retry (exceto no último)
        if attempt < WEBHOOK_MAX_RETRIES:
            delay = WEBHOOK_RETRY_DELAY * attempt  # backoff linear: 10s, 20s, 30s
            log(f"Retrying {message_id} in {delay}s...")
            time.sleep(delay)

    # TODAS AS TENTATIVAS FALHARAM
    log(f"ALL {WEBHOOK_MAX_RETRIES} attempts FAILED for {message_id}. Last error: {last_error}")
    log(f"Message {message_id} KEPT in Redis for retry on next sweep")

    # Marca no Redis que houve falha (para diagnóstico), mas NÃO deleta
    try:
        raw = redis_client.get(f"message:{message_id}")
        if raw:
            data = json.loads(raw)
            data["_lastFailure"] = datetime.utcnow().isoformat()
            data["_lastError"] = str(last_error)
            data["_failCount"] = data.get("_failCount", 0) + 1
            redis_client.set(f"message:{message_id}", json.dumps(data))
    except Exception:
        pass

    # Limpa o job em memória (será re-criado pelo sweep)
    with schedule_lock:
        scheduled_jobs.pop(message_id, None)
        try:
            schedule.clear(message_id)
        except Exception:
            pass


def schedule_message(message_id: str, schedule_timestamp: str, webhook_url: str, payload: Dict[str, Any]):
    """
    Agenda um job para executar exatamente em 'schedule_timestamp'.
    """
    with schedule_lock:
        # Cancela job anterior se existir
        if message_id in scheduled_jobs:
            schedule.clear(message_id)
            scheduled_jobs.pop(message_id, None)

        # Aceita ISO 8601 com 'Z'
        schedule_time = datetime.fromisoformat(schedule_timestamp.replace('Z', '+00:00'))
        now = datetime.now(schedule_time.tzinfo)

        if schedule_time <= now:
            # Executa imediatamente (em thread separada para não bloquear)
            log(f"Message {message_id} is in the past ({schedule_timestamp}), firing immediately")
            t = threading.Thread(
                target=fire_webhook,
                args=(message_id, webhook_url, payload),
                daemon=True
            )
            t.start()
            return

        # Converte para horário LOCAL como datetime "naive" (schedule usa localtime)
        local_dt = schedule_time.astimezone().replace(tzinfo=None)

        def job():
            fire_webhook(message_id, webhook_url, payload)
            return schedule.CancelJob

        job_instance = schedule.every().day.at(local_dt.strftime("%H:%M:%S")).do(job).tag(message_id)
        job_instance.next_run = local_dt

        scheduled_jobs[message_id] = job_instance
        log(f"Message {message_id} scheduled for {local_dt.isoformat()} (local time)")


def scheduler_worker():
    """Thread que executa os jobs pendentes a cada 1 segundo."""
    while True:
        try:
            with schedule_lock:
                schedule.run_pending()
        except Exception as e:
            log(f"Error in scheduler_worker: {e}")
        time.sleep(1)


def sweep_failed_messages():
    """
    Varredura periódica: busca mensagens no Redis cujo scheduleTo já passou
    e que não têm job em memória (falha anterior). Re-dispara imediatamente.
    """
    while True:
        try:
            time.sleep(SWEEP_INTERVAL)
            now = datetime.utcnow()
            swept = 0

            for key in redis_client.scan_iter(match="message:*", count=1000):
                try:
                    raw = redis_client.get(key)
                    if not raw:
                        continue

                    data = json.loads(raw)
                    msg_id = data.get("id")
                    schedule_to = data.get("scheduleTo")

                    if not msg_id or not schedule_to:
                        continue

                    # Verifica se já passou do horário
                    schedule_time = datetime.fromisoformat(schedule_to.replace('Z', '+00:00'))
                    schedule_utc = schedule_time.astimezone().replace(tzinfo=None)

                    if schedule_utc > now:
                        # Ainda no futuro — verifica se tem job em memória
                        with schedule_lock:
                            if msg_id not in scheduled_jobs:
                                # Perdeu o job, re-agenda
                                log(f"[SWEEP] Re-scheduling future message {msg_id} (scheduleTo: {schedule_to})")
                                schedule_message(msg_id, schedule_to, data["webhookUrl"], data["payload"])
                        continue

                    # Já passou do horário — verifica se tem job ativo
                    with schedule_lock:
                        has_job = msg_id in scheduled_jobs

                    if not has_job:
                        # Verifica rate-limit: não re-disparar se falhou há menos de 5 min
                        last_failure = data.get("_lastFailure")
                        if last_failure:
                            try:
                                last_fail_time = datetime.fromisoformat(last_failure)
                                if (now - last_fail_time).total_seconds() < 300:
                                    continue  # Espera mais antes de tentar de novo
                            except Exception:
                                pass

                        fail_count = data.get("_failCount", 0)
                        if fail_count >= 10:
                            # Muitas falhas, loga mas não tenta mais
                            continue

                        log(f"[SWEEP] Firing overdue message {msg_id} (scheduleTo: {schedule_to}, failCount: {fail_count})")
                        swept += 1
                        t = threading.Thread(
                            target=fire_webhook,
                            args=(msg_id, data["webhookUrl"], data["payload"]),
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
    """Restaura jobs a partir do Redis usando SCAN."""
    try:
        restored_count = 0
        for key in redis_client.scan_iter(match="message:*", count=1000):
            try:
                raw = redis_client.get(key)
                if not raw:
                    continue
                data = json.loads(raw)
                schedule_message(
                    data["id"],
                    data["scheduleTo"],
                    data["webhookUrl"],
                    data["payload"],
                )
                restored_count += 1
                log(f"Restored scheduled message - ID: {data['id']}")
            except Exception as e:
                log(f"Failed to restore message {key}: {e}")
        log(f"Restored {restored_count} scheduled messages from Redis")
    except Exception as e:
        log(f"Error restoring messages: {e}")


@app.on_event("startup")
def _startup():
    restore_scheduled_messages()

    # Thread do scheduler (executa jobs agendados)
    t1 = threading.Thread(target=scheduler_worker, daemon=True)
    t1.start()

    # Thread de varredura (recupera mensagens que falharam)
    t2 = threading.Thread(target=sweep_failed_messages, daemon=True)
    t2.start()

    log("Scheduler API started with retry support and sweep worker")


# =======================
# ROTAS
# =======================

@app.post("/messages")
async def create_scheduled_message(message: ScheduleMessage, token: str = Depends(verify_token)):
    try:
        redis_key = f"message:{message.id}"

        if redis_client.exists(redis_key):
            log(f"Message exists, updating - ID: {message.id}")
        else:
            log(f"Creating new message - ID: {message.id}")

        message_data = {
            "id": message.id,
            "scheduleTo": message.scheduleTo,
            "payload": message.payload,
            "webhookUrl": message.webhookUrl
        }

        redis_client.set(redis_key, json.dumps(message_data))
        log(f"Message stored in Redis - ID: {message.id}")

        schedule_message(message.id, message.scheduleTo, message.webhookUrl, message.payload)

        return {"status": "scheduled", "messageId": message.id}

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
    try:
        redis_client.ping()
        with schedule_lock:
            job_count = len(schedule.jobs)
        return {
            "status": "healthy",
            "redis": "connected",
            "scheduledJobs": job_count,
            "version": "2.0.0",
        }
    except Exception as e:
        return {"status": "unhealthy", "redis": "disconnected", "error": str(e)}


# Endpoint de diagnóstico
@app.get("/stats")
async def stats(token: str = Depends(verify_token)):
    try:
        total_redis = 0
        failed_count = 0
        overdue_count = 0
        now = datetime.utcnow()

        for key in redis_client.scan_iter(match="message:*", count=1000):
            total_redis += 1
            raw = redis_client.get(key)
            if raw:
                data = json.loads(raw)
                if data.get("_failCount"):
                    failed_count += 1
                schedule_to = data.get("scheduleTo")
                if schedule_to:
                    st = datetime.fromisoformat(schedule_to.replace('Z', '+00:00'))
                    if st.astimezone().replace(tzinfo=None) <= now:
                        overdue_count += 1

        with schedule_lock:
            job_count = len(schedule.jobs)

        return {
            "messagesInRedis": total_redis,
            "jobsInMemory": job_count,
            "failedMessages": failed_count,
            "overdueMessages": overdue_count,
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    log("Starting Scheduler API server v2.0.0")
    uvicorn.run(app, host="0.0.0.0", port=8000)
