# Scheduler API

Um serviço de agendamento de **webhooks** construído com **FastAPI**, **Redis** e a lib `schedule`.  
Desenvolvido pela **DinastIA Community**.

---

## Visão Geral

A API permite **agendar** chamadas de webhook para timestamps específicos (ISO-8601).  
Cada agendamento é persistido no **Redis** e executado **uma única vez** no horário definido.

### Como funciona

1. **Agendamento (upsert):** ao criar uma mensagem (`POST /messages`), ela é salva no Redis e (re)agendada em memória.
2. **Execução única:** no disparo do webhook, a chave é removida do Redis e o job é limpo do scheduler.
3. **Restauração automática:** ao iniciar, o serviço faz `SCAN` em `message:*` no Redis e restaura/agenda os jobs.
4. **Autenticação:** todos os endpoints (exceto `/health`) exigem **Bearer Token**.

> **Thread-safe:** o acesso ao `schedule` é protegido por `RLock`; o worker roda em **thread** iniciada no evento de **startup**.

---

## Pré-requisitos

- Python 3.x
- Redis em execução (local/remoto)
- Dependências Python:
  ```bash
  pip install -r requirements.txt
