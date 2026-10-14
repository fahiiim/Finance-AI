# Handover to the backend team — 14 Oct 2026

The AI service now reads **live data** through your `/api/internal/*` endpoints.
There is no demo, snapshot or fabricated data source left in the code.

Read this page first. Contract details are in `docs/backend-integration.md`.

---

## 1. What you need to deploy it

Set these on the AI service host, then restart it.

| Setting | Value | Notes |
|---|---|---|
| `ENVIRONMENT` | `production` | turns on the start-up safety checks |
| `BACKEND_INTEGRATION_MODE` | `backend_api` | the only supported mode |
| `BACKEND_API_BASE_URL` | `https://<your-host>/MyFi/api` | include the `/MyFi/api` prefix |
| `BACKEND_API_SERVICE_TOKEN` | your static `/api/internal/*` token | **not** the `/get/token` JWT |
| `SERVICE_JWT_SECRET` | 32+ characters | **must equal** Laravel's `AI_SERVICE_JWT_SECRET` |
| `OPENAI_API_KEY` | a working key | |
| `STRICT_DATA_OWNERSHIP` | `true` | rejects any record that does not say who owns it |
| `ALLOW_IN_MEMORY_CONVERSATIONS` | `true` | see decision A below |
| `DEBUG` | `false` | |

The service **refuses to start** in production if any of these is unsafe, and the
error message says which one.

## 2. How to check it works (5 minutes)

1. `GET /ready` → `backend_integration_mode` is `backend_api` and
   `backend_reachable` is `true`.
2. Run the contract checker with a real user id:
   `python scripts/verify_backend_contract.py --user-id <id>`
   (reads the base URL and token from the environment). Exit code `0` = all good.
3. Open `/docs`, click **Authorize**, paste a chat JWT, and send
   `POST /api/v1/chat` with `{"session_id": "t1", "message": "How much did I spend this month?"}`
   and header `X-Session-Id: t1`.

## 3. Your part — what only you can do

**Security (do first)**
- [ ] **Rotate** the internal service token and the Sanctum token used for
      `/get/token`. Both were pasted into a chat during integration.

**Decisions**
- [ ] **A. Chat history.** Your API has no endpoints to *write* conversations, so the
      service keeps history in memory: it is lost on restart and works with **one
      instance only**. Either accept that, or add `POST/DELETE` conversation
      endpoints (spec in `docs/handover-backend.md` §8) and set
      `CONVERSATION_STORAGE=backend_api`.
- [ ] **B. Per-user check.** Your service token is shared, so your API cannot tell
      which user is being asked about. The AI service verifies every record's
      `owner_user_id` itself, but if you can also reject a `user_id` that does not
      match the chat JWT's `sub`, do — defence in depth.

**Data the assistant cannot answer without** (it says "not available" today)
- [ ] Interest rate and minimum payment on every liability (currently `null`) —
      without these, "pay off debt faster" questions cannot be modelled.
- [ ] A **date range** on `/api/internal/bills` (`start`/`end` are ignored; only the
      current month is returned). The service works around it with one request per
      day, which is why some cash questions take 15–30 s.
- [ ] Investment value **history** (daily/monthly) if you want performance
      questions answered, and index data if you want market comparisons.
- [ ] Which field marks a transaction as **"excluded"** in the app. Testers refer to
      it, and no such field is in the API today.
- [ ] Retirement-designated account balances, if "how much have I saved for
      retirement?" should have a number.

**Test data**
- [ ] Remove the QA entries in user 267's account (for example a $100,000
      "spending efficiency" expense and a $96,358 "Addd" income). The app counts
      them, so they distort "largest purchase", "typical spending" and savings answers.

## 4. Behaviours you should know

- **Spending excludes transfers.** A row is left out only when it is flagged
  `is_review_transfer` **and** categorised `TRANSFER_IN`/`TRANSFER_OUT`. Your
  `financial-summary` counts them as expenses, so the assistant's "spent" figure
  will be lower than that endpoint's — by design, so every answer uses one definition.
- **Hand-typed rows** follow the category (`direction`), not the amount's sign;
  imported rows keep Plaid's sign.
- **Net worth and savings progress** come straight from your endpoints, so they
  agree with the app.
- **Isolation.** Every request carries only the authenticated user's id, and every
  returned record is re-checked against it.

## 5. Quality status

- 469 automated tests passing; type-check and lint clean.
- All 85 questions from the QA workbook were answered from live data; the ones that
  cannot be answered fully are the data gaps listed above.
- Verified against one real user only. Please run the contract checker for two or
  three other users (including one with no investments or bills) before go-live.
