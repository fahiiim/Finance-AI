# Assumptions

This build started from a detailed specification but **no actual
repository, existing backend code, API contract, or SQL schema was
attached to the request**. Per the spec's own instruction ("do not invent
missing business rules — document it as an assumption"), everything below
is explicit rather than silently guessed.

## Repository / requirements audit (Phase 1 result)

- `/mnt/user-data/uploads` was empty at build time — no existing repo,
  `CLAUDE.md`, documentation, or database export to inspect.
- Nothing in this build overwrites or conflicts with an existing codebase,
  since none was provided. Before deploying against a real system, the
  items below need to be reconciled against the actual backend.

## Backend integration

- **`backend_api` mode's endpoint paths and query parameters** (see
  `docs/backend-integration.md`) are invented, matching the general shape
  described in the spec (section 5/47). Update
  `app/services/backend_client.py:BackendAPIClient` to match the real
  contract.
- **`read_only_db` mode is not implemented.** No MariaDB/MySQL schema was
  provided (spec section 47 asks to map `plaid_transactions`,
  `plaid_accounts`, `budgets`, `bills`, `subscriptions`,
  `recurring_transactions`, investment/liability tables, financial-score
  tables) — implementing this without inventing a schema would mean
  guessing column names and types, which the spec explicitly says not to
  do. `get_backend_client()` raises a clear `RuntimeError` pointing here if
  this mode is selected.
- **Tests** use an in-memory test double (`tests/support/in_memory_backend.py`)
  that generates deterministic, per-user-seeded data. It is not part of the
  application and cannot be selected at runtime.

## Authentication

- The service-to-service handshake (`Authorization: Bearer <JWT>` signed
  with a shared `SERVICE_JWT_SECRET`, asserting `sub`/`session_id`/
  `timezone`/`currency`) is an assumed contract — no existing auth
  mechanism was described. See `docs/security.md` for why this was chosen
  and what to replace if the real backend uses something else (mTLS,
  gateway-injected headers, asymmetric JWT, etc.).

## Conversation storage

- `InMemoryConversationStore` is a **process-local, non-persistent**
  default (spec section 11 anticipates the backend may already own
  `chat_sessions`/`chat_messages`). It's correct for local dev/tests but
  will not survive a restart or work across multiple service instances.
  Replace with a `ConversationStore` implementation against the real
  backend/database before production use.

## Financial calculations

- **Spending spike methodology** (`app/tools/analytics.py`): flag a day
  when its spending exceeds the period's daily mean by ≥1.5 standard
  deviations AND is at least 50 units of the account currency. This
  threshold is configurable and documented in-code (spec section 30
  explicitly says not to invent a "validated" methodology and to document
  the assumption instead).
- **Forecasting methodology** (`app/tools/forecasting.py`): simple trailing
  average daily spending, projected linearly forward. No seasonality or
  trend detection. Every forecast result is marked `is_estimate: true`
  with an explicit methodology note.
- **Affordability analysis** (`app/orchestrator/intent_router.py`): reuses
  the planner's generic `max_amount` field to carry "the amount the user is
  asking about" (e.g. "can I afford $500?" → `max_amount=500`), since no
  dedicated field was specified. Checks available checking-account balance
  minus unpaid bills due in the next 14 days.
- **Financial score**: never recomputed. If the backend doesn't return one,
  the tool reports `availability: missing` rather than inventing a score
  (spec section 32).

## Currency

- Multi-currency accounts are detected (`app/utils/money.py:
  require_same_currency`) and raise a validation error rather than silently
  summing mismatched currencies. True multi-currency support (conversion,
  display formatting per locale) was out of scope without a specified
  requirement.

## What was deliberately left out (spec section 51)

No vector database, RAG, multi-agent framework, LangChain/LangGraph, or
extra microservices/queues were introduced — the core system is FastAPI +
OpenAI + typed tools + deterministic calculations + conversation context,
as the spec requests.

## Rate limiting

`RATE_LIMIT_REQUESTS_PER_MINUTE` exists in configuration but no limiter
middleware is wired in, since the spec asks for "hooks" and the actual
infrastructure (Redis, gateway-level limiting, etc.) wasn't specified.
