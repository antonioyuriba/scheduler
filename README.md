# Scheduler API

Um serviço de agendamento de webhooks construído com FastAPI, Redis e a biblioteca `schedule`. Desenvolvido pela DinastIA Community.

## Visão Geral

Esta API permite agendar chamadas de webhook para timestamps específicos. As mensagens são persistidas no Redis e executadas exatamente uma vez no horário definido.

### Como Funciona

1. Agendamento: ao criar uma mensagem, ela é salva no Redis e adicionada ao scheduler interno.
2. Execução única: o job executa no horário definido e, após a execução do webhook, a mensagem é removida do Redis e o job é cancelado.
3. Persistência/Restore: em caso de reinício do servidor, todas as mensagens salvas no Redis são restauradas e re-agendadas automaticamente.
4. Autenticação: todos os endpoints (exceto `/health`) exigem Bearer Token.

## Pré-requisitos

- Python 3.x
- Redis em execução (local ou remoto)
- Dependências Python (instalar com `pip install -r requirements.txt`)

## Setup de Ambiente

Crie um arquivo `.env` com:

```env
# Redis Configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

# API Configuration
API_HOST=0.0.0.0
API_PORT=8000
API_TOKEN=your-secret-token-here
```

Observações:
- Não faça commit do `.env`. Use o `.env.example` como referência.
- Em produção, injete as variáveis de ambiente via orquestrador/secret store.

## Executando o Servidor

```bash
python scheduler_api.py
```

O servidor sobe em `http://localhost:8000`.

## Autenticação

Todos os endpoints (exceto `/health`) exigem Bearer token:

```
Authorization: Bearer your-secret-token-here
```

## API Endpoints

### Schedule a Message

`POST /messages`

**Headers:**
- `Authorization: Bearer your-secret-token-here`
- `Content-Type: application/json`

**Body:**
```json
{
  "id": "unique-message-id",
  "scheduleTo": "2024-12-25T10:30:00Z",
  "payload": {
    "data": "your webhook payload"
  },
  "webhookUrl": "https://your-webhook-endpoint.com"
}
```

**Response:**
```json
{
  "status": "scheduled",
  "messageId": "unique-message-id"
}
```

### List Scheduled Messages

`GET /messages`

**Headers:**
- `Authorization: Bearer your-secret-token-here`

**Response:**
```json
{
  "scheduledJobs": [
    {
      "messageId": "unique-message-id",
      "nextRun": "2024-12-25T10:30:00",
      "job": "<function job at 0x...>"
    }
  ],
  "count": 1
}
```

### Delete a Scheduled Message

`DELETE /messages/{message_id}`

**Headers:**
- `Authorization: Bearer your-secret-token-here`

**Response:**
```json
{
  "status": "deleted",
  "messageId": "unique-message-id"
}
```

### Health Check

`GET /health`

No authentication required.

**Response:**
```json
{
  "status": "healthy",
  "redis": "connected"
}
```

## Error Codes

- `401` - Missing or invalid authentication token
- `404` - Message not found (when deleting)
- `409` - Message with ID already exists (when creating)
- `500` - Internal server error

## About DinastIA Community

This project is developed by **DinastIA Community**, the largest AI Agents community in Brazil, dedicated to advancing artificial intelligence and automation technologies.

## License

This project is licensed under the MIT License.
