# Updates for the dev team — 3 Sep 2026

Read this first, then `docs/launch-readiness.md` for the full picture.

---

## 🔴 Two blockers. Nothing works until both are fixed.

### 1. `BACKEND_API_SERVICE_TOKEN` is empty

This is the cause of the error you reported:

> *"Your income figures are temporarily unavailable because the data service did not respond."*

An empty value still builds a working HTTP client and sends `Authorization: Bearer `
(nothing after "Bearer") on **every** call. Laravel rejects all of them, and the AI
service correctly refuses to invent numbers rather than showing a made-up figure.

```diff
- BACKEND_API_SERVICE_TOKEN=
+ BACKEND_API_SERVICE_TOKEN=<the value Laravel checks the header against>
```

It must be **the same string on both sides**.

**This is not an AI bug.** The metadata in your own response proves the rest worked:
`intent: INCOME_QUERY` was correct, `period_phrase: "this month"` resolved to
`2026-09-01 → 2026-09-02`, and `data_used: false` says no data was read. Planning,
date resolution and routing were all fine — only the fetch failed.

### 2. The `OPENAI_API_KEY` in `.env` is rejected by OpenAI

Verified with a live call: `401 Incorrect API key provided`. Put a working key in.

> ⚠️ **The key and `SERVICE_JWT_SECRET` were pasted into a chat. Rotate both.**
> Treat any credential that has been shared in a message, ticket or screenshot as
> burned.

`BACKEND_API_BASE_URL` is **correct** — no change needed. It resolves properly to
`https://<your-backend-host>/MyFi/api/internal/transactions`.

---

## 🟢 How to tell it is fixed — one check

```
GET /ready
```

- **200** → this instance can genuinely answer. Go.
- **503** → it cannot, and the body names why.

```json
{
  "status": "not_ready",
  "problems": ["OpenAI key rejected or unreachable (AuthenticationError)"],
  "checks": {
    "openai_api_key_configured": true,
    "openai_key_valid": false,
    "backend_credentials_configured": false,
    "backend_reachable": true
  }
}
```

Note `openai_api_key_configured: true` alongside `openai_key_valid: false` — the
endpoint separates *configured* from *actually working*, because a revoked key passes
every "is it set?" test and fails every real request.

Wire your orchestrator's **readiness** probe to `/ready` and its **liveness** probe to
`/health`. Do not point liveness at `/ready`: one Laravel outage would then restart the
whole fleet, which fixes nothing.

---

## What changed in this update

### Startup now refuses to boot on a broken configuration

Previously these were only checked when `ENVIRONMENT=production`, and you were running
`local` against a real backend — so the guard existed and never ran. **That was my
mistake.** It now runs in every environment:

```
BACKEND_INTEGRATION_MODE=backend_api is misconfigured:
BACKEND_API_SERVICE_TOKEN is empty (every backend call would be sent
unauthenticated, and the backend will reject all of them)
```

A misconfiguration now fails the deploy instead of reaching users.

### Logs finally show *why* a call failed

With `JSON_LOGS=false` the formatter printed only the message, so you saw
`backend_request_failed` with no path and no status code — the two fields that tell
"endpoint missing" from "token wrong". Now, in both plain and JSON format:

```
2026-09-03 07:29 ERROR app.repositories.base: backend_request_failed | path=/internal/transactions status=401
```

| Log line | Cause |
|---|---|
| `backend_request_error` | Cannot connect — wrong URL, DNS, port, firewall |
| `backend_request_failed` + `status=404` | Endpoint not implemented |
| `backend_request_failed` + `status=401`/`403` | Token wrong or empty ← **your case** |
| `backend_request_timeout` | Laravel too slow — raise `BACKEND_API_TIMEOUT_SECONDS` |

Secrets stay redacted in both formats.

### `/ready` became a real probe

It used to return `200 "ready"` while every question failed. It now checks that the
OpenAI key is *accepted* (a free `models.list()` call) and that the backend *responds*,
both cached ~15s so a fleet of probes costs nothing.

### A date bug that would have started on 1 September

`"1 - 15 August"` resolved its start against **today's** month, producing start = 1 Sep,
end = 15 Aug — an end before its start, discarded as unresolvable. Users typing an
explicit date range were asked to clarify a range they had stated exactly.

It worked correctly all through August and broke the moment the month rolled over.
Fixed, with the clock pinned in the test so it cannot pass by luck of the calendar again.

### Docker

- `HEALTHCHECK` added, `--proxy-headers` for running behind a load balancer.
- `scripts/` now ships in the image, so you can run the contract checker from inside it
  against the same network the service sees.
- **`--workers 1`, deliberately.** Conversation history is in process memory until
  `CONVERSATION_STORAGE=backend_api`. A second worker is a second memory, so a user's
  follow-up turn can hit a worker that never heard of them — the assistant re-asks a
  question it was already answered. Nothing errors. Raise it only after conversations
  are persisted.

### Other

- `.env.production.example` — every setting with its reasoning, no secrets.
- `.gitignore` had `.env.*`, which was silently excluding `.env.example` itself. Fixed.
- Earlier in this pass: restaurant/category matching, merchant name matching, the
  `is_review_transfer` correction, missing-vs-zero, backend timeouts no longer
  returning HTTP 500. See `docs/launch-readiness.md` §1.

**403 tests passing · ruff clean · mypy clean across 71 files.**

---

## Verify your endpoints before users do

```bash
python scripts/verify_backend_contract.py \
    --base-url https://<your-backend-host>/MyFi/api \
    --token "$BACKEND_API_SERVICE_TOKEN" \
    --user-id 267
```

Exits `0` when correct, `1` otherwise, so it can gate a pipeline. Run it per endpoint
as you build. It catches the four things that otherwise fail **silently in production**:

| Check | Why it matters |
|---|---|
| **Sign convention** | Your data uses Plaid's rule — **positive = money out**. If Laravel normalises that when serving JSON, income and spending swap places in every figure, with no error anywhere. |
| `owner_user_id` | Without it the cross-user containment check cannot verify anything. |
| `subcategory` | Without it, "how much on restaurants?" widens to all food & drink. |
| Inclusive dates | `start=end=2026-08-20` must return that day, or every single-date question answers "nothing". |

---

## Minimum to go live

Only three endpoints are required. The other eight may 404 at first — those features
report as *unavailable* rather than as zero.

- `GET /internal/transactions` — params `user_id`, `start`, `end`, `merchant`
- `GET /internal/accounts` — param `user_id`
- `GET /internal/financial-summary` — params `user_id`, `start`, `end`

Full request/response shapes: `docs/handover-backend.md` §5.

**The AI never touches the database.** It calls back to Laravel per question:

```
App → Laravel /api/chat (your auth)
        → AI /api/v1/chat (service JWT + X-Session-Id)
            → Laravel /internal/transactions?user_id=267&start=…&end=…
            ← answer
```

The mobile app must not call the AI service directly — it would need the shared secret.
Laravel proxies it; the PHP is in `docs/handover-backend.md` §9.
