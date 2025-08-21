```markdown
# Scheduler API

Um serviço de agendamento de **webhooks** construído com **FastAPI**, **Redis** e a lib `schedule`.  
Desenvolvido pela **DinastIA Community**.

---

## Visão Geral

A API permite **agendar** chamadas de webhook para timestamps no padrão **ISO-8601**.  
Cada agendamento é persistido no **Redis** e executado **uma única vez** no horário definido.

### Como funciona

1. **Agendamento (upsert):** ao criar uma mensagem (`POST /messages`), ela é salva no Redis e (re)agendada em memória.
2. **Execução única:** no disparo do webhook, a chave é removida do Redis e o job é limpo do scheduler.
3. **Restauração automática:** no startup, o serviço faz `SCAN` em `message:*` no Redis e restaura/agenda os jobs.
4. **Autenticação:** todos os endpoints (exceto `/health`) exigem **Bearer Token**.
5. **Thread-safe:** o acesso ao `schedule` é protegido por `RLock`; o worker roda em **thread** iniciada no evento de **startup**.

---

## Pré-requisitos

- Python 3.x  
- Redis em execução (local/remoto)
- Dependências:
  ```bash
  pip install -r requirements.txt
```

* * *

## Configuração de Ambiente

Crie um arquivo .env:

```bash
# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

# API
API_HOST=0.0.0.0
API_PORT=8000
API_TOKEN=your-secret-token-here
```

> Observações

*   Não faça commit do .env (use .env.example como referência).
*   Em produção, injete variáveis via orquestrador/secret store.
*   O script executa por padrão em 0.0.0.0:8000.

* * *

## Executando

```plain
python scheduler_api.py
```

A API ficará disponível em [http://localhost:8000](http://localhost:8000).

* * *

## Autenticação

Inclua o header:

```sql
Authorization: Bearer your-secret-token-here
```

* * *

## Endpoints

### 1) Criar/Atualizar Agendamento (UPSERT)

POST /messages

Headers

*   Authorization: Bearer {API\_TOKEN}
*   Content-Type: application/json

Body

```json
{
  "id": "unique-message-id",
  "scheduleTo": "2025-12-25T10:30:05Z",
  "payload": { "data": "your webhook payload" },
  "webhookUrl": "https://your-webhook-endpoint.com"
}
```

Resposta

```json
{ "status": "scheduled", "messageId": "unique-message-id" }
```

> Se o id já existir, o registro no Redis é sobrescrito e o job anterior é substituído (upsert).

* * *

### 2) Listar Jobs em Memória

GET /messages

Headers

*   Authorization: Bearer {API\_TOKEN}

Resposta

```json
{
  "scheduledJobs": [
    {
      "messageId": "unique-message-id",
      "nextRun": "2025-12-25T07:30:05-03:00",
      "job": "<function job at 0x...>"
    }
  ],
  "count": 1
}
```

> Lista apenas o que está no scheduler em memória (snapshot protegido por lock).

* * *

### 3) Obter Agendamento por ID

GET /messages/{message\_id}

Headers

*   Authorization: Bearer {API\_TOKEN}

Resposta (200)

```json
{
  "id": "unique-message-id",
  "scheduleTo": "2025-12-25T10:30:05Z",
  "payload": { "data": "your webhook payload" },
  "webhookUrl": "https://your-webhook-endpoint.com"
}
```

Erros

*   404 se o id não existir no Redis.

* * *

### 4) Buscar Agendamentos (prefix/contains)

GET /messages/search

Headers

*   Authorization: Bearer {API\_TOKEN}

Query (ao menos um)

*   prefix: filtra por prefixo do ID (eficiente). Ex.: {idconta}\_{numero}
*   contains: filtra por substring em qualquer parte do ID

Exemplos

*   GET /messages/search?prefix=123\_5511999999999
*   GET /messages/search?contains=\_24h

Resposta

```json
{
  "count": 3,
  "messages": [
    {
      "id": "123_5511999999999_2h",
      "scheduleTo": "2025-12-25T08:30:00Z",
      "payload": { "...": "..." },
      "webhookUrl": "https://example.com/hook",
      "nextRun": "2025-12-25T05:30:00-03:00"
    }
  ]
}
```

> Quando prefix é usado, a busca aplica SCAN MATCH message:{prefix}\*.  
> nextRun aparece se houver job em memória com a mesma tag id.

* * *

### 5) Deletar por ID

DELETE /messages/{message\_id}

Headers

*   Authorization: Bearer {API\_TOKEN}

Resposta

```json
{ "status": "deleted", "messageId": "unique-message-id" }
```

> Remove a chave no Redis e limpa o job (se existir) do scheduler.

* * *

### 6) Deleção em Lote (prefix/contains)

DELETE /messages/bulk
DELETE /messages/bulk/ _(aceita barra final para evitar redirects)_

Headers

*   Authorization: Bearer {API\_TOKEN}

Query (ao menos um)

*   prefix: ex. {idconta}\_{numero}
*   contains: ex. \_24h

Exemplos

*   DELETE /messages/bulk?prefix=123\_5511999999999
*   DELETE /messages/bulk?contains=\_24h

Resposta

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

> Dica: para IDs iniciando com “+55…”, codifique o “+” na URL como %2B (ex.: prefix=%2B55119...) para evitar que alguns clientes convertam “+” em espaço.

> Observação: o código também aceita POST /messages/bulk como fallback (não exibido no Swagger), caso algum proxy bloqueie DELETE.

* * *

### 7) Health Check

GET /health _(sem autenticação)_

Resposta

```json
{ "status": "healthy", "redis": "connected" }
```

* * *

## Exemplos com cURL

*   Buscar por prefixo

```powershell
curl -H "Authorization: Bearer $API_TOKEN" \
  "http://localhost:8000/messages/search?prefix=123_5511999999999"
```

*   *   Deletar em lote por prefixo

```powershell
curl -X DELETE -H "Authorization: Bearer $API_TOKEN" \
  "http://localhost:8000/messages/bulk?prefix=123_5511999999999"
```

*   *   Buscar por substring

```powershell
curl -H "Authorization: Bearer $API_TOKEN" \
  "http://localhost:8000/messages/search?contains=_12h"
```

*   *   Deletar em lote por substring

```powershell
curl -X DELETE -H "Authorization: Bearer $API_TOKEN" \
  "http://localhost:8000/messages/bulk?contains=_24h"
```

* * *

## Códigos de Erro

*   400 — Filtro ausente (em /messages/search e /messages/bulk quando nenhum filtro é informado)
*   401 — Token ausente ou inválido
*   404 — Mensagem não encontrada (em GET /messages/{id})
*   500 — Erro interno

* * *

## Observações Importantes

*   Formato & timezone: scheduleTo aceita ISO-8601 (ex.: 2025-12-25T10:30:05Z).
*   Restauração: no startup, o serviço varre message:\* (via SCAN) e re-agenda os jobs.
*   Execução única: após disparo do webhook, a mensagem é removida do Redis e o job é limpo.
*   Retries: não há retentativas por padrão; se necessário, implemente backoff/idempotência no destino.
*   Escala horizontal: várias réplicas podem duplicar disparos se todas restaurarem/rodarem jobs. Em produção, considere 1 instância “scheduler” ou lock distribuído no Redis.
*   Segurança: proteja o API\_TOKEN e não versione secrets.

* * *

## Sobre a DinastIA Community

Projeto criado pela DinastIA Community, a maior comunidade de AI Agents do Brasil, dedicada a avançar IA e automação.

* * *

## Licença

MIT
