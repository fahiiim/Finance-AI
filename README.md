# Financial AI Service

A production-oriented FastAPI AI orchestration layer for a personal finance
application. It answers natural-language questions about a user's own
verified financial data — spending, income, bills, budgets, subscriptions,
investments, liabilities, and more — using a deterministic
retrieve-then-calculate-then-explain pipeline (never an LLM guessing at
numbers).

**Scope:** this repository is the AI service only. It does not include a
frontend, mobile app, or the client's existing financial backend — see
`docs/architecture.md` and `docs/assumptions.md`.

## Architecture

```
Client's existing backend
    -> FastAPI AI Service
        -> Authentication (trusted UserContext)
        -> Conversation Manager
        -> Intent Detection + Execution Planner
        -> Financial Tool Router
        -> Backend Data Client
        -> Deterministic Calculation / Analytics Engine
        -> Minimal verified-data Context Builder
        -> OpenAI
        -> Response Validation
    -> Structured JSON response
```

Full detail: `docs/architecture.md`.

## Requirements

- Python 3.12+
- An OpenAI API key (to exercise `/api/v1/chat` end to end)
- The backend's internal API base URL and service token

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — at minimum set OPENAI_API_KEY and SERVICE_JWT_SECRET
```

## Running locally

```bash
uvicorn app.main:app --reload
```

- Swagger UI: http://localhost:8000/docs
- OpenAPI schema: http://localhost:8000/openapi.json
- Liveness: http://localhost:8000/health
- Readiness: http://localhost:8000/ready

The service reads financial data from the live backend through its internal
API (`BACKEND_INTEGRATION_MODE=backend_api`, with `BACKEND_API_BASE_URL` and
`BACKEND_API_SERVICE_TOKEN` set). See `docs/backend-integration.md` for the
contract.

## Authentication

Every endpoint except `/health` and `/ready` requires a service-signed JWT:

```
Authorization: Bearer <token signed with SERVICE_JWT_SECRET, claims: sub, session_id, timezone, currency>
X-Session-Id: <session id>
```

See `docs/security.md` for the full contract and the user-isolation
invariant this enforces.

## Environment variables

See `.env.example` for the full list with inline documentation. Key ones:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` / `OPENAI_MODEL` | LLM credentials + model (fully configurable, no hardcoded model name) |
| `BACKEND_INTEGRATION_MODE` | `backend_api` (the live internal API) |
| `BACKEND_API_BASE_URL` / `BACKEND_API_SERVICE_TOKEN` | Used when mode is `backend_api` |
| `SERVICE_JWT_SECRET` | Verifies the trusted identity token from the existing backend |
| `CONVERSATION_RECENT_MESSAGE_LIMIT` / `CONVERSATION_SUMMARY_TRIGGER_LENGTH` | Context-window strategy |

## Testing

```bash
pytest
ruff check .
mypy app
```

See `docs/testing.md` for what's covered, including the critical
cross-user data isolation suite, and `tests/evaluation/dataset.json` for
the behavioral evaluation cases.

## Project structure

```
app/
    main.py                  FastAPI app, middleware, centralized error handling
    api/routes/               chat.py, conversations.py, health.py (thin routes)
    core/                     config, security (UserContext), logging, exceptions
    schemas/                  chat, user, conversation, financial, responses, planner
    orchestrator/              planner, intent_router, context_builder, orchestrator
    llm/                       provider interface + OpenAI implementation
    prompts/                   versioned prompt text
    tools/                     typed financial data tools + deterministic calculations/analytics/forecasting
    services/                  backend client abstraction, conversation storage, memory, per-request cache
    repositories/               base HTTP repository
    utils/                     date resolution, money math, validation/injection-defense
tests/
    unit/                      calculations, dates, security, planner, injection-defense, user isolation
    integration/                 API-level tests (chat, conversations, health)
    evaluation/                dataset.json — behavioral eval cases
docs/                          architecture, backend-integration, api, security, prompts, assumptions, testing
```

## Backend integration

See `docs/backend-integration.md` for the verified integration contract. The
data source is chosen in one place
(`app/services/backend_client.py:get_backend_client`).

## Prompt architecture

See `docs/prompts.md`. All prompt text lives in `app/prompts/`, versioned
(`_V1`, `_V2`, ...), never inline in application code.

## Security

See `docs/security.md` for the full model: user isolation, the auth
handshake, prompt injection defense, data minimization, and secret
handling.

## Known limitations / remaining backend dependencies

See `docs/assumptions.md` for the complete list. In short: `read_only_db`
mode isn't implemented (no schema provided), the assumed `backend_api`
endpoint contract needs reconciling against the real backend, conversation
storage defaults to an in-memory store, and rate-limiting middleware isn't
wired in (configuration is ready; infrastructure choice wasn't specified).
