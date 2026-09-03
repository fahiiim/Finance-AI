# Testing the AI Service Locally

Everything here was run against a live server and the responses below are
real output, not illustrations.

## 0. Prerequisites

`.env` is already configured for local testing:

- `BACKEND_INTEGRATION_MODE=mock` — deterministic fake financial data seeded
  per `user_id`. No real backend or database needed.
- `SERVICE_JWT_SECRET` — a locally generated dev secret. **Never used in
  production.**
- `OPENAI_API_KEY` — set, and verified working against `gpt-4.1`.
  Confirm with `GET /ready` -> `"openai_api_key_configured": true`.
  Note that flag only checks the value is non-empty; a *wrong* key still
  reads true and fails at the OpenAI call with `502 llm_provider_error`.

Start the service:

```powershell
python -m uvicorn app.main:app --reload
```

Local dev boots with in-memory conversation storage. Production refuses to
start that way unless `ALLOW_IN_MEMORY_CONVERSATIONS=true` is set
deliberately — see `docs/handover-backend.md` §8.

---

## 1. Minting a token

In production the Laravel backend mints these (handover §3). For local
testing use the helper, which reads the secret from `.env` so the token is
always valid for the running service:

```powershell
python scripts/dev_token.py                          # default user 42
python scripts/dev_token.py --user 99 --session s2   # a second user
python scripts/dev_token.py --curl                   # ready-to-run commands
```

Negative-test tokens, for checking that auth actually rejects things:

```powershell
python scripts/dev_token.py --expired        # exp an hour in the past
python scripts/dev_token.py --no-exp         # no exp claim at all
python scripts/dev_token.py --wrong-secret   # signed with the wrong key
```

A minted token decodes to:

```json
{
  "sub": "42",
  "session_id": "session-alpha",
  "timezone": "America/New_York",
  "currency": "USD",
  "iat": 1787379518,
  "exp": 1787379818
}
```

`sub` is the value that in production **must** be `application_users.id`.

---

## 2. Health check — no auth

```bash
curl http://127.0.0.1:8000/health
```
```json
{"status":"ok","service":"financial-ai-service"}
```

---

## 3. Conversations

```powershell
$T = python scripts/dev_token.py --user 42 --session session-alpha
```

**Create** (`201`):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/conversations \
  -H "Authorization: Bearer $T" -H "X-Session-Id: session-alpha"
```
```json
{"session_id":"session-alpha","status":"created"}
```

**Read back** (`200`):

```bash
curl http://127.0.0.1:8000/api/v1/conversations/session-alpha \
  -H "Authorization: Bearer $T" -H "X-Session-Id: session-alpha"
```
```json
{"session_id":"session-alpha","user_id":"42","messages":[],"summary":null}
```

---

## 4. Authentication rejections — all verified

| Test | Result |
|---|---|
| No `Authorization` header | `401 authentication_error` — "Missing or malformed Authorization header." |
| Expired token | `401` — "The service token has expired." |
| Token with no `exp` claim | `401` — "The service token is missing a required claim." |
| Signed with the wrong secret | `401` — "Invalid or expired service token." |

Note the third row: a token **without** an expiry is rejected, not accepted
forever. That is the behaviour the backend must satisfy.

---

## 5. Chat

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Authorization: Bearer $T" \
  -H "X-Session-Id: session-alpha" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "session-alpha", "message": "How much did I spend last month?"}'
```

Real verified response:

```json
{
  "session_id": "session-alpha",
  "message": {
    "role": "assistant",
    "content": "You spent $3,143.00 last month. This total covers 27 transactions from July 1 to July 31, 2026. Your top spending categories were Groceries ($878.00), Subscriptions ($471.00)..."
  },
  "metadata": {
    "intent": "SPENDING_ANALYSIS",
    "tools_used": ["get_expenses", "calculate_total_spending"],
    "data_used": true,
    "calculation_used": true,
    "needs_clarification": false
  }
}
```

`calculation_used: true` is the point worth noticing — the arithmetic was
done in Python, and the model only phrased the result.

If you get `502 llm_provider_error`, the process is holding a stale or
invalid key. `.env` is read at startup only, so **restart after editing it**;
`--reload` watches `.py` files, not `.env`.

Other messages, all verified:

```json
{"session_id": "session-alpha", "message": "What is my checking balance?"}
{"session_id": "session-alpha", "message": "How much did I spend on groceries last month?"}
{"session_id": "session-alpha", "message": "What subscriptions am I paying for?"}
{"session_id": "session-alpha", "message": "Can I afford to spend $500 this week?"}
{"session_id": "session-alpha", "message": "Compare my spending this month to last month"}
{"session_id": "session-alpha", "message": "What is my financial score?"}
```

**Body `session_id` must equal the `X-Session-Id` header**, otherwise:

```json
{"code":"validation_failed","message":"session_id in the request body does not match the authenticated session."}
```

---

## 6. The test that matters: cross-user isolation

Two users, the **same** session id string:

```powershell
$A = python scripts/dev_token.py --user 42 --session shared-session
$B = python scripts/dev_token.py --user 99 --session shared-session
```

User 42 creates it:

```json
{"session_id":"shared-session","status":"created"}
```

User 99 asks for the identical session id:

```json
{"code":"conversation_not_found","message":"No conversation found for this session."}
```
`404` — **not** user 42's conversation. Conversations are keyed by
`(user_id, session_id)`, so knowing a session id is never enough.

Once `OPENAI_API_KEY` is set, the stronger check is to ask both users the
same question and confirm the answers differ — mock data is seeded per
`user_id`, so user 42 and user 99 have entirely separate finances.

---

## 7. Automated tests

```powershell
python -m pytest -q          # 279 tests
python -m ruff check .
python -m mypy app
```

Against a running server:

```powershell
python scripts/smoke_test.py          # 22 endpoint / auth / isolation checks
python scripts/smoke_test.py --chat   # adds the LLM path
```

The security-critical suites:

| File | Covers |
|---|---|
| `tests/unit/test_user_isolation.py` | data is requested for the right user |
| `tests/unit/test_cross_user_containment.py` | data returned is *verified* to belong to that user |
| `tests/integration/test_cross_user_end_to_end.py` | a deliberately leaking backend produces an error, not an answer |
| `tests/unit/test_token_hardening.py` | expiry, signature, lifetime, algorithm |
| `tests/unit/test_prompt_injection.py` | retrieved data cannot escape the prompt envelope |
| `tests/unit/test_sign_convention.py` | income is never reported as spending |
| `tests/unit/test_rate_limit.py` | per-user throttling |
| `tests/unit/test_runtime_safety.py` | startup guardrails and config parsing |
| `tests/unit/test_affordability.py` | the sum a user names is not confused with a filter, nor with an estimated price |
| `tests/unit/test_category_scoping.py` | a category question is answered with that category's number |
| `tests/unit/test_clarification_loop.py` | the assistant never asks the same question twice |
| `tests/unit/test_dates.py` | every period phrase resolves the same way every time |
| `tests/unit/test_prompt_versions.py` | a new prompt version keeps every guarantee of the one it replaces |
| `tests/unit/test_period_defaults.py` | an ordinary question with no period stated is answered, not rejected |
| `tests/unit/test_daily_breakdown.py` | per-day figures cover every day, zeros included, and long windows are capped |
| `tests/unit/test_sqlite_backend.py` | the real-data client scopes every query and drops no rows |
