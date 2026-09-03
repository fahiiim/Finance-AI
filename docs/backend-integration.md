# Backend Integration Contract

## Status: assumed, not confirmed

No existing repository, backend API contract, or SQL schema was provided
when this service was built (see `docs/assumptions.md`). Everything below
is this service's expectation of what it needs from the existing backend —
review and adjust to match the real system before enabling `backend_api`
mode in production.

## Integration modes

Set via `BACKEND_INTEGRATION_MODE`:

| Mode | Description | Status |
|---|---|---|
| `mock` | Deterministic in-memory data, seeded per `user_id`. Local dev / tests only. | Implemented (`app/services/mock_backend_client.py`) |
| `backend_api` | Calls the existing backend's internal API over HTTPS. **Primary recommendation.** | Implemented (`app/services/backend_client.py: BackendAPIClient`), assumed endpoint paths — see below |
| `read_only_db` | Direct read-only/replica DB connection. | **Not implemented.** No schema was provided. Implementing this requires the real MariaDB/MySQL schema for `plaid_transactions`, `plaid_accounts`, `budgets`, `bills`, `subscriptions`, `recurring_transactions`, investment/liability tables, and financial-score tables — see spec section 47. Add `app/services/db_backend_client.py` implementing `BackendClient` once that schema is available; nothing else in the pipeline needs to change. |

Switching modes is a configuration change only (`get_backend_client()` in
`app/services/backend_client.py` is the single factory point) — no
orchestrator or tool code depends on which mode is active.

## Assumed `backend_api` endpoint contract

`BackendAPIClient` assumes the following internal endpoints exist on
`BACKEND_API_BASE_URL`, each accepting `user_id` as a query parameter that
the backend is expected to **independently verify** against its own
service-token identity (never trust the AI service's claim of `user_id`
without that backend-side check):

```
GET /internal/users/profile?user_id=...
GET /internal/accounts?user_id=...
GET /internal/transactions?user_id=...&start=...&end=...&category=...&merchant=...&min_amount=...&max_amount=...
GET /internal/bills?user_id=...&start=...&end=...
GET /internal/budgets?user_id=...&start=...&end=...
GET /internal/subscriptions?user_id=...
GET /internal/recurring-transactions?user_id=...
GET /internal/investments?user_id=...
GET /internal/liabilities?user_id=...
GET /internal/financial-summary?user_id=...&start=...&end=...
GET /internal/financial-score?user_id=...
```

Response bodies are expected to be JSON arrays/objects matching the shapes
in `app/schemas/financial.py` (`Account`, `Transaction`, `Bill`,
`BudgetCategory`, `Subscription`, `RecurringTransaction`,
`InvestmentHolding`, `Liability`, `FinancialSummary`, `FinancialScore`).
Field names in `app/schemas/financial.py` will need adjusting (or a mapping
layer added to `BackendAPIClient`) once the real API contract is known.

## Conversation storage

Per spec section 11: if the existing backend already owns `chat_sessions` /
`chat_messages` tables, implement `ConversationStore` (see
`app/services/conversation_service.py`) against the backend API instead of
the default `InMemoryConversationStore`, and wire it into
`get_conversation_store()`. No orchestrator code needs to change.

## Authentication handshake

See `docs/security.md` for the full contract. In short: the existing
backend authenticates the end user, then calls this service with a
service-signed JWT (`Authorization: Bearer ...`) asserting `sub` (user_id)
and `session_id`. This service never accepts a user_id from anywhere else.
