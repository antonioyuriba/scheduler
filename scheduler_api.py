import json
import os
import time
import threading
from datetime import datetime
from typing import Dict, Any
import redis
import requests
import schedule
from fastapi import FastAPI, HTTPException, Depends, Header, Query
from pydantic import BaseModel
import uvicorn
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Scheduler API", version="1.0.0")

API_TOKEN = os.getenv('API_TOKEN')

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
    decode_responses=True
)

class ScheduleMessage(BaseModel):
    id: str
    scheduleTo: str
    payload: Dict[str, Any]
    webhookUrl: str

# Dicionário para controlar jobs agendados por ID
scheduled_jobs = {}

def _build_next_run_map():
    """
    Retorna um dict {message_id: nextRunIsoStringOrNone} baseado nos jobs do schedule.
    """
    next_run_map = {}
    for job in schedule.jobs:
        try:
            tag_id = list(job.tags)[0] if job.tags else None
            if tag_id:
                next_run_map[tag_id] = job.next_run.isoformat() if job.next_run else None
        except Exception:
            pass
    return next_run_map

def _iter_message_keys_by_filter(prefix: str | None = None, contains: str | None = None):
    """
    Itera sobre chaves message:* no Redis aplicando filtros:
    - prefix: usa SCAN com MATCH message:{prefix}*
    - contains: faz SCAN geral e filtra por substring no id
    """
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
            if contains in msg_id:
                yield key
        except Exception:
            pass

def fire_webhook(message_id: str, webhook_url: str, payload: Dict[str, Any]):
    try:
        response = requests.post(webhook_url, json=payload, timeout=30)
        response.raise_for_status()
        print(f"Webhook fired successfully for message {message_id}")
    except Exception as e:
        print(f"Failed to fire webhook for message {message_id}: {e}")
    finally:
        redis_client.delete(f"message:{message_id}")
        print(f"Message {message_id} cleaned from Redis")
        # Remove job do dicionário após execução
        if message_id in scheduled_jobs:
            scheduled_jobs.pop(message_id, None)
            schedule.clear(message_id)

def schedule_message(message_id: str, schedule_timestamp: str, webhook_url: str, payload: Dict[str, Any]):
    # Cancela job agendado anterior se existir
    if message_id in scheduled_jobs:
        schedule.clear(message_id)
        scheduled_jobs.pop(message_id)

    schedule_time = datetime.fromisoformat(schedule_timestamp.replace('Z', '+00:00'))
    current_time = datetime.now(schedule_time.tzinfo)
    
    if schedule_time <= current_time:
        fire_webhook(message_id, webhook_url, payload)
        return
    
    def job():
        fire_webhook(message_id, webhook_url, payload)
        return schedule.CancelJob
    
    job_instance = schedule.every().day.at(schedule_time.strftime("%H:%M")).do(job).tag(message_id)
    scheduled_jobs[message_id] = job_instance

def scheduler_worker():
    while True:
        schedule.run_pending()
        time.sleep(1)

def restore_scheduled_messages():
    try:
        keys = redis_client.keys("message:*")
        restored_count = 0
        
        for key in keys:
            try:
                message_data = json.loads(redis_client.get(key))
                message_id = message_data["id"]
                schedule_to = message_data["scheduleTo"]
                webhook_url = message_data["webhookUrl"]
                payload = message_data["payload"]
                
                schedule_message(message_id, schedule_to, webhook_url, payload)
                restored_count += 1
                print(f"[{datetime.now().isoformat()}] Restored scheduled message - ID: {message_id}")
                
            except Exception as e:
                print(f"[{datetime.now().isoformat()}] Failed to restore message {key}: {e}")
        
        print(f"[{datetime.now().isoformat()}] Restored {restored_count} scheduled messages from Redis")
        
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error restoring messages: {e}")

restore_scheduled_messages()

scheduler_thread = threading.Thread(target=scheduler_worker, daemon=True)
scheduler_thread.start()

@app.post("/messages")
async def create_scheduled_message(message: ScheduleMessage, token: str = Depends(verify_token)):
    try:
        redis_key = f"message:{message.id}"
        
        if redis_client.exists(redis_key):
            print(f"[{datetime.now().isoformat()}] Message exists, updating - ID: {message.id}")
        else:
            print(f"[{datetime.now().isoformat()}] Creating new message - ID: {message.id}")

        message_data = {
            "id": message.id,
            "scheduleTo": message.scheduleTo,
            "payload": message.payload,
            "webhookUrl": message.webhookUrl
        }
        
        redis_client.set(redis_key, json.dumps(message_data))
        print(f"[{datetime.now().isoformat()}] Message stored in Redis - ID: {message.id}")
        
        schedule_message(message.id, message.scheduleTo, message.webhookUrl, message.payload)
        
        return {"status": "scheduled", "messageId": message.id}
    
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error in create: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to schedule message: {str(e)}")

@app.delete("/messages/{message_id}")
async def delete_scheduled_message(message_id: str, token: str = Depends(verify_token)):
    try:
        redis_key = f"message:{message_id}"
        
        redis_client.delete(redis_key)
        
        try:
            schedule.clear(message_id)
        except ValueError:
            print(f"[{datetime.now().isoformat()}] No schedule found for ID: {message_id}")
        
        scheduled_jobs.pop(message_id, None)
        
        return {"status": "deleted", "messageId": message_id}
    
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error in delete: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete message: {str(e)}")


@app.get("/messages")
async def list_scheduled_messages(token: str = Depends(verify_token)):
    try:
        jobs = []
        for job in schedule.jobs:
            jobs.append({
                "messageId": list(job.tags)[0] if job.tags else "unknown",
                "nextRun": job.next_run.isoformat() if job.next_run else None,
                "job": str(job.job_func)
            })
        
        return {"scheduledJobs": jobs, "count": len(jobs)}
    
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error listing jobs: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list jobs: {str(e)}")

# Novos endpoints: busca e deleção em lote por filtros de ID (prefix/contains)
@app.get("/messages/search")
async def search_messages(
    prefix: str | None = Query(default=None, description="Filtra por prefixo do ID, ex.: {idconta}_{numero}"),
    contains: str | None = Query(default=None, description="Filtra por substring contida no ID"),
    token: str = Depends(verify_token),
):
    """
    Lista mensagens salvas no Redis aplicando filtros por ID:
    - prefix: busca eficiente via SCAN MATCH (message:{prefix}*)
    - contains: busca geral e filtra no cliente
    Retorna também o nextRun caso exista job agendado em memória.
    """
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
                    "nextRun": next_run_map.get(msg_id)
                })
            except Exception as e:
                print(f"[{datetime.now().isoformat()}] Failed to parse message {key}: {e}")

        return {"count": len(results), "messages": results}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error in search_messages: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to search messages: {str(e)}")

@app.get("/messages/{message_id}")
async def get_scheduled_message(message_id: str, token: str = Depends(verify_token)):
    """
    Retrieves the full data of a specific scheduled message from Redis.
    """
    try:
        redis_key = f"message:{message_id}"
        message_data_json = redis_client.get(redis_key)
        
        if not message_data_json:
            raise HTTPException(status_code=404, detail=f"Message with ID '{message_id}' not found")
            
        # Carrega a string JSON para um dicionário Python e a retorna
        return json.loads(message_data_json)
        
    except HTTPException:
        # Re-lança a exceção HTTP para que o FastAPI a trate corretamente
        raise
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error in get_scheduled_message: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve message: {str(e)}")

@app.delete("/messages/bulk")
async def bulk_delete_messages(
    prefix: str | None = Query(default=None, description="Filtra por prefixo do ID, ex.: {idconta}_{numero}"),
    contains: str | None = Query(default=None, description="Filtra por substring contida no ID"),
    token: str = Depends(verify_token),
):
    """
    Apaga em lote mensagens que combinem com os filtros de ID e limpa os jobs agendados.
    """
    try:
        if not prefix and not contains:
            raise HTTPException(status_code=400, detail="Informe ao menos um filtro: 'prefix' ou 'contains'.")

        deleted_ids = []
        for key in list(_iter_message_keys_by_filter(prefix=prefix, contains=contains)):
            try:
                message_id = key.split("message:", 1)[1]
            except Exception:
                continue

            redis_client.delete(key)

            try:
                schedule.clear(message_id)
            except ValueError:
                pass
            except Exception as e:
                print(f"[{datetime.now().isoformat()}] Failed to clear schedule for {message_id}: {e}")

            scheduled_jobs.pop(message_id, None)
            deleted_ids.append(message_id)

        return {"deleted": len(deleted_ids), "messageIds": deleted_ids}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error in bulk_delete_messages: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to bulk delete messages: {str(e)}")
@app.get("/health")
async def health_check():
    try:
        redis_client.ping()
        return {"status": "healthy", "redis": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "redis": "disconnected", "error": str(e)}

if __name__ == "__main__":
    print(f"[{datetime.now().isoformat()}] Starting Scheduler API server")
    uvicorn.run(app, host="0.0.0.0", port=8000)
