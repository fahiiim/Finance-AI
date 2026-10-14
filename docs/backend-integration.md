# Backend Integration Contract

## Status: verified against the live service

The `backend_api` mode below was checked against
`https://<backend-host>/MyFi/api/internal/*` with real data, and
`scripts/verify_backend_contract.py` passes every check. The contract is
documented by the backend team in *AI Service Integration - Backend
Clarifications Answered*; this page records what this service relies on.

## Integration modes

Set via `BACKEND_INTEGRATION_MODE`:

| Mode | Description | Status |
|---|---|---|
| `backend_api` | Calls the backend's internal API, which reads the live database. **Production mode.** | Implemented and verified (`app/services/backend_client.py: BackendAPIClient`, mapping in `app/services/live_api_mapping.py`) |
| `read_only_db` | Direct database connection. | Not implemented; not needed |

## Authentication — two separate credentials

| Credential | Used by | For |
|---|---|---|
| Chat JWT (from `/get/token`, HS256, 5 min) | Laravel → this service | `/api/v1/chat`, `/api/v1/conversations` |
| `BACKEND_API_SERVICE_TOKEN` (static) | This service → Laravel | `/api/internal/*` |

The chat JWT is **rejected** by `/api/internal/*` (401). The service token is
static, identifies this *service* rather than a user, and never expires — rotate
it through the secret manager, and never log it or put it in a client.

Because the token is not per-user, the backend cannot verify `user_id` for us.
This service therefore re-checks every record's `owner_user_id` against the
requesting user (`app/core/data_isolation.py`) and aborts on a mismatch.

## Request shape

```
GET {BACKEND_API_BASE_URL}/internal/<endpoint>?user_id=<id>[&...]
Authorization: Bearer <BACKEND_API_SERVICE_TOKEN>
Accept: application/json
```

`BACKEND_API_BASE_URL` includes the `/MyFi/api` prefix. Every response is an
envelope — `{"status", "message", "data", "meta"}` — and a non-2xx `status` is
treated as an error, never as "no data".

## Endpoints used

| Endpoint | Used for | Notes |
|---|---|---|
| `users/profile` | currency, timezone | |
| `accounts` | balances | types: checking, savings, credit, manually |
| `transactions` | spending, income, merchants, categories | **Paginated**, max `per_page=500`; filters `start`, `end`, `merchant`, `min_amount`, `max_amount` |
| `bills` | bills due / paid | Current month only; `selected_date` returns one day's bills |
| `budgets` | budget usage | accepts `start`/`end` |
| `subscriptions`, `recurring-transactions` | subscriptions, recurring | same stream ids; the latter carries the category |
| `investments`, `liabilities` | holdings, debts | liabilities also returns checking/brokerage rows |
| `net-worth` | net worth | the figure the app displays |
| `allo-score` | Allo score + history | `financial-score` returns the same data |
| `credit-score-history` | credit score + history | heavy payload; use `limit` |
| `savings-goals` | goal + backend progress | |
| `liability-history` | debt trend | |
| `retirement-readiness` | retirement | |

`conversations` is read-only, so chat history stays in this service
(`CONVERSATION_STORAGE`).

## Rules this service applies

These are decisions about meaning, not formatting; each has a test in
`tests/unit/test_live_api_client.py`.

- **Sign.** `amount` is an absolute value; `amount_signed` keeps Plaid's sign
  (positive = money out). This service reads `amount_signed`.
- **Transfers.** A row is excluded from spending/income only when it is
  `is_review_transfer` **and** its category is `TRANSFER_IN`/`TRANSFER_OUT`.
  `is_internal_transfer` is also set on payroll-tax debits and ordinary
  purchases, so it is not used on its own.
- **Summaries are derived from transactions**, not from `financial-summary`,
  which counts transfers as expenses. One definition of "expense" everywhere.
- **Bills** are paid (`completed_bills`) or unpaid (`upcoming_bills`,
  `due_bills`). Windows past month end are completed day by day via
  `selected_date`; windows before the current month are reported unavailable.
- **Net worth and savings progress** use the backend's figures so the
  assistant agrees with the app.
- **Subscriptions** are monthly outflows, excluding transfers, loan payments,
  fees, taxes and ACH autopays (`DES:`), with exact repeats collapsed.
- **Liabilities** keep only debt types; a brokerage or checking row returned
  by the endpoint is not debt.
- **Cash** is checking + savings. Credit balances are money owed and never
  added to it.

## Verify before relying on it

```
python scripts/verify_backend_contract.py --user-id <id> [--other-user-id <id>]
```

Run it after every backend deploy. It exits non-zero on a failure.

## Known limits

- No per-user token: isolation depends on the `owner_user_id` re-check.
- `credit-score-history` is slow (hundreds of KB per row); the service asks
  for `limit=1` when only the current score is needed. A slim mode on the
  backend would remove the cost.
- Bills before the current month are not available from the backend.
