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

Authorization: Bearer your-secret-token-here


## Endpoints

### 1) Criar/Atualizar um Agendamento (UPSERT)
`POST /messages`

- Headers:
  - `Authorization: Bearer {API_TOKEN}`
  - `Content-Type: application/json`

- Body:
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

- Resposta:
```json
{
  "status": "scheduled",
  "messageId": "unique-message-id"
}
```

Notas:
- Se o `id` já existir, o registro no Redis é sobrescrito (upsert) e o job anterior é substituído.

### 2) Listar Jobs Agendados em Memória
`GET /messages`

- Headers:
  - `Authorization: Bearer {API_TOKEN}`

- Resposta:
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

Notas:
- Lista o que está no scheduler em memória. Pode haver items salvos no Redis que ainda não foram (re)carregados em memória imediatamente após restart (o restore ocorre no startup).

### 3) Obter Detalhes de um Agendamento por ID
`GET /messages/{message_id}`

- Headers:
  - `Authorization: Bearer {API_TOKEN}`

- Resposta (200):
```json
{
  "id": "unique-message-id",
  "scheduleTo": "2024-12-25T10:30:00Z",
  "payload": { "data": "your webhook payload" },
  "webhookUrl": "https://your-webhook-endpoint.com"
}
```

- Erros:
  - `404` se o `id` não existir em Redis.

### 4) Buscar Agendamentos por Filtro (Prefixo/Substring)
`GET /messages/search`

- Headers:
  - `Authorization: Bearer {API_TOKEN}`

- Query params (ao menos um):
  - `prefix`: filtra por prefixo do ID (mais eficiente). Ex.: `{idconta}_{numero}`
  - `contains`: filtra por substring contida no ID (mais genérico)

- Exemplos:
  - `GET /messages/search?prefix=123_5511999999999`
  - `GET /messages/search?contains=_24h`

- Resposta:
```json
{
  "count": 3,
  "messages": [
    {
      "id": "123_5511999999999_2h",
      "scheduleTo": "2024-12-25T08:30:00Z",
      "payload": { "..." : "..." },
      "webhookUrl": "https://example.com/hook",
      "nextRun": "2024-12-25T05:30:00-03:00"
    }
  ]
}
```

Notas:
- Quando `prefix` é usado, a busca é feita via SCAN com `MATCH` em Redis: `message:{prefix}*` (eficiente).
- Quando `contains` é usado, a busca varre com SCAN e filtra no cliente (adequado para volumes moderados).
- `nextRun` aparece se existir job em memória para aquele `id`.

### 5) Deletar um Agendamento por ID
`DELETE /messages/{message_id}`

- Headers:
  - `Authorization: Bearer {API_TOKEN}`

- Resposta:
```json
{
  "status": "deleted",
  "messageId": "unique-message-id"
}
```

### 6) Deleção em Lote por Filtro (Prefixo/Substring)
`DELETE /messages/bulk`

- Headers:
  - `Authorization: Bearer {API_TOKEN}`

- Query params (ao menos um):
  - `prefix`: ex.: `{idconta}_{numero}`
  - `contains`: ex.: `_24h`

- Exemplos:
  - `DELETE /messages/bulk?prefix=123_5511999999999`
  - `DELETE /messages/bulk?contains=_24h`

- Resposta:
```json
{
  "deleted": 3,
  "messageIds": [
    "123_5511999999999_2h",
    "123_5511999999999_12h",
    "123_5511999999999_24h"
  ]
}
```

Notas:
- Remove as chaves no Redis e limpa jobs no scheduler (se existirem).
- Para seu caso típico, prefira `prefix={idconta}_{numero}` para pegar “_2h”, “_12h”, “_24h”, etc.

### 7) Health Check
`GET /health`

- Sem autenticação.
- Resposta:
```json
{
  "status": "healthy",
  "redis": "connected"
}
```

## Exemplos com cURL

- Buscar por prefixo:
```bash
curl -H "Authorization: Bearer $API_TOKEN" "http://localhost:8000/messages/search?prefix=123_5511999999999"
```

- Deleção em lote por prefixo:
```bash
curl -X DELETE -H "Authorization: Bearer $API_TOKEN" "http://localhost:8000/messages/bulk?prefix=123_5511999999999"
```

- Buscar por substring:
```bash
curl -H "Authorization: Bearer $API_TOKEN" "http://localhost:8000/messages/search?contains=_12h"
```

- Deleção em lote por substring:
```bash
curl -X DELETE -H "Authorization: Bearer $API_TOKEN" "http://localhost:8000/messages/bulk?contains=_24h"
```

## Códigos de Erro

- `400` - Filtro ausente (em `/messages/search` e `/messages/bulk` quando nenhum filtro é informado)
- `401` - Token ausente ou inválido
- `404` - Mensagem não encontrada (em `GET /messages/{id}`)
- `500` - Erro interno

## Observações Importantes

- Timezone: `scheduleTo` aceita ISO8601 (ex.: com `Z` para UTC). O `schedule` opera com hora local; certifique-se de alinhar timezone ou normalize tudo em UTC para evitar discrepâncias.
- Restore: na inicialização, o serviço restaura chaves `message:*` do Redis via SCAN e re-agenda os jobs.
- Execução única: após disparar o webhook, a mensagem é removida do Redis e o job é limpo.
- Retentativas: atualmente, não há retries no webhook. Avalie adicionar backoff + idempotência se o endpoint de destino for sensível a falhas transitórias.
- Escalabilidade horizontal: várias réplicas podem tentar disparos duplicados. Para produção, considere separar o worker da API e adotar lock distribuído no Redis.
- Segurança: não commitar `.env`; utilize variáveis de ambiente no deploy.

## Licença

Projeto licenciado sob MIT.
