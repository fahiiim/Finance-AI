# API Field Reference — every endpoint, every field

Generated from the service's own OpenAPI schema. Live spec:
`http://127.0.0.1:8000/openapi.json` · Swagger UI: `/docs`

The service exposes **six** operations across five paths. The eleven `/internal/*` endpoints in
`docs/handover-backend.md` are what the **Laravel backend** must build; they
are not part of this service.

---

## Common headers

| Header | Where | Required | Value |
|---|---|---|---|
| `Authorization` | all `/api/v1/*` | yes | `Bearer <jwt>` |
| `X-Session-Id` | all `/api/v1/*` | yes¹ | conversation id |
| `Content-Type` | POST with a body | yes | `application/json` |
| `X-Request-Id` | any | no | echoed back; supply your own for tracing |

¹ May instead come from the token's `session_id` claim.

### JWT claims

| Claim | Required | Value |
|---|---|---|
| `sub` | **yes** | user id — in production **`application_users.id`** |
| `session_id` | yes¹ | conversation id |
| `exp` | **yes** | expiry. Absent ⇒ rejected. Lifetime must be ≤ 900s |
| `iat` | recommended | issued-at; enables the max-lifetime check |
| `timezone` | no | IANA name, e.g. `America/New_York`. Defaults `UTC` |
| `currency` | no | defaults `USD` |
| `aud` / `iss` | no | validated only if configured on the service |

---

## 1. `GET /health`

No auth. Liveness only.

```json
{"status": "ok", "service": "financial-ai-service"}
```

## 2. `GET /ready`

No auth. Readiness probe — reports what the service is actually wired to:

```json
{
  "status": "ready",
  "checks": {
    "openai_api_key_configured": false,
    "backend_integration_mode": "mock"
  }
}
```

Useful as a first check: if `backend_integration_mode` says `mock`, you are
looking at fake data, not your database.

---

## 3. `POST /api/v1/chat`

The main endpoint.

### Request fields

| Field | Type | Required | Constraints |
|---|---|---|---|
| `session_id` | string | **yes** | 1–128 chars. **Must equal `X-Session-Id`** |
| `message` | string | **yes** | 1–2000 chars, non-blank after trimming |

There is **no `user_id` field, deliberately.** Adding one to the body does
nothing — identity comes only from the token. Sending one is ignored.

```json
{
  "session_id": "session-alpha",
  "message": "How much did I spend on groceries last month?"
}
```

### Response fields (`200`)

| Field | Type | Notes |
|---|---|---|
| `session_id` | string | echoed |
| `message.role` | string | always `assistant` |
| `message.content` | string | the user-facing answer |
| `metadata.intent` | enum | one of the 23 values below |
| `metadata.tools_used` | string[] | which tools ran, e.g. `["get_expenses"]` |
| `metadata.data_used` | bool | whether financial data reached the model |
| `metadata.calculation_used` | bool | whether a deterministic calc ran |
| `metadata.needs_clarification` | bool | the reply is a clarifying question |
| `metadata.period_start` | date, nullable | first day the answer covers (`null` when the question needed no period) |
| `metadata.period_end` | date, nullable | last day the answer covers, inclusive |
| `metadata.period_phrase` | string, nullable | the wording the period came from, e.g. `"last month"` |

```json
{
  "session_id": "session-alpha",
  "message": { "role": "assistant", "content": "You spent $312.40 on groceries in July." },
  "metadata": {
    "intent": "CATEGORY_ANALYSIS",
    "tools_used": ["get_expenses", "calculate_category_total"],
    "data_used": true,
    "calculation_used": true,
    "needs_clarification": false,
    "period_start": "2026-07-01",
    "period_end": "2026-07-31",
    "period_phrase": "last month"
  }
}
```

Every figure in a financial answer is only meaningful next to its period.
`period_start` / `period_end` say exactly which window the numbers cover, so
a caller never has to parse the prose to find out — or to notice the wrong
window was used. Both are `null` for a reply that needed no period at all
(a greeting, a clarifying question, an account-balance lookup).

### Date phrases the service understands

The `message` is free text — these are resolved deterministically in the
user's own timezone, never by the model.

| Kind | Examples |
|---|---|
| Relative | `today`, `yesterday`, `this week`, `last week`, `this month`, `last month`, `last 30 days`, `this quarter`, `this year`, `last year` |
| Weekday | `last friday`, `friday`, `most recent friday`, `last mon`/`tue`/`wed`… |
| Exact date | `August 20`, `Aug 20`, `20 August`, `20th August`, `August 20, 2024`, `2026-08-20`, `the 20th` |
| Exact range | `August 1 to August 15`, `between Aug 1 and Aug 15`, `August 1 to 15`, `1 - 15 August` |
| Comparison | `this month vs last month`, `last month vs this month`, `the current and previous calendar months` |
| Recurring weekday | `fridays in june`, `every monday last month`, `all fridays in June 2026` |

Two behaviours worth knowing:

- **A year you don't give is inferred backwards.** `December 20` asked in
  August means *last* December — this assistant answers questions about money
  already spent, not about dates that have not happened.
- **A day of the month that hasn't arrived steps back.** On the 23rd, `the
  31st` means last month's 31st.

A period-scoped question with **no** period at all (`what is my dining
budget?`) assumes the current month and says so in the answer.

### Day-by-day data

When the question is about individual days (`each day`, `day by day`, a
specific date), the answer carries one figure per day, **including days with
no spending** — a zero is a real answer, and a missing row is not the same as
a quiet day.

Windows longer than **62 days** get no daily series: a year of rows would
crowd out everything else. The total and the category split are still
returned, and the assistant says the breakdown isn't available for that
window rather than truncating one.

### Status codes

| Code | Meaning |
|---|---|
| `200` | answered |
| `401` | missing / invalid / expired token |
| `413` | message body over the byte limit |
| `422` | validation failed, or body `session_id` ≠ header |
| `429` | rate limited (default 60/min per user) |
| `502` | OpenAI or backend returned an error |
| `503` | financial data temporarily unavailable |
| `504` | OpenAI or backend timed out |

---

## 4. `POST /api/v1/conversations`

No body. Session comes from the header/token.

`201` → `{"session_id": "session-alpha", "status": "created"}`
`401` unauthenticated · `409` already exists

## 5. `GET /api/v1/conversations/{session_id}`

The path addresses the conversation; the token authorizes it. You can only
ever reach **your own** sessions.

| Field | Type |
|---|---|
| `session_id` | string |
| `user_id` | string |
| `messages[].role` | `user` \| `assistant` \| `system` |
| `messages[].content` | string |
| `messages[].created_at` | ISO-8601 or null |
| `messages[].intent` | string or null |
| `summary` | object or null |

```json
{"session_id": "session-alpha", "user_id": "42", "messages": [], "summary": null}
```

`404` when the session does not exist **for you** — including when it exists
for somebody else.

## 6. `DELETE /api/v1/conversations/{session_id}`

`204` on success, and also when there was nothing to delete (idempotent).

---

## Intent values

`metadata.intent` is one of:

```
GENERAL_CONVERSATION   TRANSACTION_QUERY        SPENDING_ANALYSIS
INCOME_QUERY           BALANCE_QUERY            BILL_QUERY
BUDGET_QUERY           SUBSCRIPTION_QUERY       RECURRING_EXPENSE_QUERY
CATEGORY_ANALYSIS      MERCHANT_ANALYSIS        SPENDING_SPIKE
PERIOD_COMPARISON      SAVINGS_ANALYSIS         CASH_FLOW_ANALYSIS
AFFORDABILITY_ANALYSIS FORECAST_QUERY           INVESTMENT_QUERY
LIABILITY_QUERY        FINANCIAL_SCORE_QUERY    FINANCIAL_CALCULATION
OVERALL_FINANCIAL_HEALTH                        UNKNOWN
```

### Messages that exercise each intent

Intent is chosen by the planner LLM, so these are the *expected* routes, not
guaranteed ones — verify with `metadata.intent` in the response. They are
untested here because `OPENAI_API_KEY` is not set.


| Intent | Message |
|---|---|
| `BALANCE_QUERY` | What is my checking account balance? |
| `TRANSACTION_QUERY` | Show me my transactions from last week |
| `SPENDING_ANALYSIS` | How much did I spend last month? |
| `INCOME_QUERY` | How much did I earn last month? |
| `CATEGORY_ANALYSIS` | How much did I spend on groceries last month? |
| `MERCHANT_ANALYSIS` | How much have I spent at Netflix this year? |
| `SUBSCRIPTION_QUERY` | What subscriptions am I paying for? |
| `RECURRING_EXPENSE_QUERY` | What recurring payments do I have? |
| `BILL_QUERY` | What bills are due this month? |
| `BUDGET_QUERY` | Am I over budget on dining? |
| `PERIOD_COMPARISON` | Compare my spending this month to last month |
| `SPENDING_SPIKE` | Did I have any unusual spending days last month? |
| `SAVINGS_ANALYSIS` | What is my savings rate? |
| `CASH_FLOW_ANALYSIS` | What is my cash flow this month? |
| `AFFORDABILITY_ANALYSIS` | Can I afford to spend $500 this week? |
| `FORECAST_QUERY` | How much will I spend next month? |
| `INVESTMENT_QUERY` | How are my investments doing? |
| `LIABILITY_QUERY` | How much debt do I have? |
| `FINANCIAL_SCORE_QUERY` | What is my financial score? |
| `OVERALL_FINANCIAL_HEALTH` | How is my overall financial health? |
| `GENERAL_CONVERSATION` | Hello, what can you help me with? |

---

## Error envelope

Every error uses the same shape — never a stack trace, never another user's
data:

```json
{
  "code": "authentication_error",
  "message": "The service token has expired.",
  "request_id": "edabdb21-46af-431b-85e2-a0bb1725928e"
}
```

| `code` | HTTP |
|---|---|
| `authentication_error` | 401 |
| `user_context_error` | 401 |
| `authorization_error` | 403 |
| `conversation_not_found` | 404 |
| `conversation_error` | 409 |
| `request_too_large` | 413 |
| `validation_failed` | 422 |
| `rate_limited` | 429 |
| `cross_user_data_blocked` | 500 |
| `backend_service_error` / `llm_provider_error` / `invalid_model_response` | 502 |
| `financial_data_unavailable` | 503 |
| `backend_timeout` / `llm_timeout` | 504 |

---

## Running the checks

```powershell
python -m uvicorn app.main:app --reload   # terminal 1
python scripts/smoke_test.py              # terminal 2  -> 22 checks
python scripts/smoke_test.py --chat       # adds the LLM path
```

Exit code is 0 only if everything passed, so it drops straight into CI.
