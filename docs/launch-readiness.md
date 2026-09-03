# Finance AI — Launch Readiness & Handover

**Status as of 3 Sep 2026**

**The code is ready to hand over. The system is not ready to launch** — and the gap
is entirely on the Laravel side. Everything the AI service owes is done, tested and
type-clean. It cannot read a single row of live data until the `/internal/*`
endpoints exist.

| | |
|---|---|
| **403** | tests passing (up from 279). Ruff clean, mypy clean across 71 source files. |
| **3** | endpoints required. Transactions, accounts, financial-summary. None exist yet — the AI is blind without them. |
| **4** | decisions your team still owns. Sign convention, ownership field, transfer rule, test rows in production. |

---

## 1. Fixed in this codebase

All verified against the real 122,326-row production dump, not fixtures.

| Fix | What was wrong |
|---|---|
| **"Restaurant" returns restaurant spending** | Data is labelled `Food And Drink`; the filter was an exact string match, so "restaurants" returned a confident **$0.00**. Now resolves the category and narrows to the restaurant rows inside it. Same for "groceries", "takeout", "eating out", "coffee". |
| **Merchant names match how people type them** | Statements read `CHECKCARD 0531 SMITHS-FOOD #4349 LAS VEGAS NV XXXXX…`, so "Smith's" and "Wendy's" matched nothing. Every spelling now returns the same total, grouped into one row. An unknown name is reported as not found, never as $0.00. |
| **Real purchases were being silently discarded** | `is_review_transfer` was treated as "this is a transfer", but only 911 of 2,252 flagged rows are. Excluding all of them understated one user's food spending by **45%** ($1,139 reported vs $2,084 actual) and made KFC, Little Caesars and Cheesecake Summerlin vanish. Now excluded only when the category agrees. |
| **Missing data no longer reported as zero** | Investments returned "$0 market value", debts "$0", and debt-to-income "0.0%" beside $42,035 of real card balances. All now return `availability: missing` with null values. |
| **A backend timeout no longer returns HTTP 500** | The most likely production incident surfaced as a red `500 tool_execution_error` mid-chat. Now a conversational reply: "temporarily unavailable, try again shortly." |
| **Request cost cut for the heaviest questions** | The savings streak pulled a year of raw transactions on every savings question — 584 KB against a 10-second timeout. Now concurrent monthly aggregates. |
| **Cross-user check closed a partial-labelling hole** | Strict mode only failed when a response was *entirely* unlabelled — one labelled record vouched for every unlabelled one beside it, which is exactly the shape of a partial backend rollout. |
| **A third party's name was leaking into subscriptions** | ACH descriptors carry the recipient's full legal name after `INDN:` and reached subscription lists verbatim. Now stripped. |

### Supporting changes

- `"last 2 months"`, `"last 3 months"`, `"past year"`, `"last 2 weeks"` did not resolve
  at all and became clarifying questions. Counted month windows were also spanning
  three calendar months for "last 2 months", inflating those totals by half.
- Merchant and subscription names are cleaned of card-network noise for display, so
  what the user is shown is a name they can type back and get a match on.
- `data_used` was reported `true` on failed reads — any dashboard built on that metric
  would have counted outages as successful data reads.

---

## 2. Payload per question, before and after

Measured against a Laravel-shaped HTTP backend serving one real user's rows.

| Question | Calls | Before | After |
|---|---:|---:|---:|
| Savings rate + streak | 13 | 584.4 KB | **2.1 KB** |
| Merchant not found | 2 | 275.6 KB | **14.5 KB** |
| Spending analysis | 1 | 14.5 KB | 14.5 KB |
| Period comparison | 2 | 56.7 KB | 56.7 KB |
| Overall financial health | 3 | 0.2 KB | 0.2 KB |

---

## 3. The launch sequence

Ordered, because each step depends on the one before it. **Steps 1 and 2 are the
launch blockers.**

### Step 1 — Laravel builds the three required endpoints
**Owner: Laravel dev** · Contract in `docs/handover-backend.md` §5

- `GET /internal/transactions` — with `start`, `end`, `merchant` query params
- `GET /internal/accounts`
- `GET /internal/financial-summary` — with `start`, `end`

Each scoped by `user_id`. The other eight endpoints (`bills`, `budgets`,
`subscriptions`, `recurring-transactions`, `investments`, `liabilities`,
`financial-score`, `users/profile`) may 404 at first — those features report as
*unavailable* rather than as zero.

**The AI never touches the database.** It calls back to Laravel per question:

```
App → Laravel /api/chat (your auth)
        → AI /api/v1/chat  (service JWT + X-Session-Id)
            → Laravel /internal/transactions?user_id=267&start=…&end=…
            → Laravel /internal/accounts?user_id=267
            ← answer
```

The app must not call the AI service directly — it would need the shared secret.
Laravel proxies it; the PHP is in `docs/handover-backend.md` §9.

### Step 2 — Run the contract checker against them
**Owner: Laravel dev** · Run it per endpoint, as they build

```bash
python scripts/verify_backend_contract.py \
    --base-url https://your-laravel-host \
    --token "$BACKEND_API_SERVICE_TOKEN" \
    --user-id 267
```

Exits `0` when the shape is right, `1` otherwise, so it can gate a pipeline. It
catches the four things that otherwise fail **silently** in production: sign
convention, missing `owner_user_id`, missing `subcategory`, and non-inclusive date
bounds.

### Step 3 — Point the AI service at the backend
**Owner: whoever deploys**

Set the environment variables in section 4. Startup now refuses to boot on a missing
token or base URL, so a misconfiguration fails the deploy instead of 401-ing every
user.

### Step 4 — Turn on strict ownership once the logs are quiet
**Owner: Laravel dev, then deploy** · Post-launch is acceptable

The service logs `records_without_owner_user_id` **once per endpoint** that omits the
field. When those lines stop appearing, set `STRICT_DATA_OWNERSHIP=true` — you will
know it is safe rather than guessing.

### Step 5 — Move conversations into the database
**Owner: Laravel dev** · Required before scaling out

Five routes plus three `chat_messages` columns (`intent`, `is_clarification`,
`date_phrase` — see `docs/handover-backend.md` §8.1). The AI side is already built and
tested; flip `CONVERSATION_STORAGE=backend_api`.

**Until then you must run single-instance.** In-memory history is not shared across
instances, so behind a load balancer a user's second turn can hit a process that never
heard of them. Nothing errors — the assistant just forgets mid-conversation.

---

## 4. Production configuration

Copy **`.env.production.example`** and fill in the four secrets. It documents every
setting and the reasoning behind each default.

The values that matter most:

```bash
ENVIRONMENT=production
BACKEND_INTEGRATION_MODE=backend_api
BACKEND_API_BASE_URL=https://your-laravel-host      # a path is fine: .../MyFi/api
BACKEND_API_SERVICE_TOKEN=<must match what Laravel checks>
BACKEND_API_TIMEOUT_SECONDS=30

# Until the conversation endpoints exist — SINGLE INSTANCE, SINGLE WORKER
ALLOW_IN_MEMORY_CONVERSATIONS=true
CONVERSATION_STORAGE=memory

# Flip to true once records_without_owner_user_id stops appearing
STRICT_DATA_OWNERSHIP=false
```

> **`BACKEND_API_SERVICE_TOKEN` must not be empty.** An empty value still builds a
> working client and sends `Authorization: Bearer ` on every call. The backend rejects
> all of them and the assistant answers *"your figures are temporarily unavailable"* to
> every question. This is the first thing the integration hit. Startup now refuses to
> boot on it — **in every environment**, not just production, because the first
> occurrence was with `ENVIRONMENT=local` pointed at a real backend.

Startup validation also rejects a short `SERVICE_JWT_SECRET`, `DEBUG=true`, and the
`mock` and `sqlite` backends in production.

### Health and readiness probes

| Probe | Endpoint | Meaning |
|---|---|---|
| Liveness | `GET /health` | Process is up. Never touches the backend — tying liveness to a dependency turns one backend outage into a fleet-wide restart loop. |
| Readiness | `GET /ready` | This instance can actually answer. Returns **503** when credentials are missing, the backend is unreachable, **or the OpenAI key is rejected**. Both dependency probes are cached ~15s so a fleet of probes generates no meaningful load. |

`/ready` distinguishes *configured* from *working*. An `OPENAI_API_KEY` that is present
but revoked — after a rotation or a leak — passes a `bool()` check and fails every chat
request, which is the same shape as the empty backend token. It is validated with a
free `models.list()` call:

```json
{ "status": "not_ready",
  "problems": ["OpenAI key rejected or unreachable (AuthenticationError)"],
  "checks": { "openai_api_key_configured": true, "openai_key_valid": false } }
```

Point the orchestrator's liveness probe at `/health` and its readiness probe at
`/ready`. `/ready` previously reported `200 "ready"` even with an empty backend token,
so nothing was monitoring the one thing that was broken.

### Running it

The `Dockerfile` runs **one worker deliberately** and documents why: a second worker is
a second process with its own in-memory conversation store, so a user's follow-up turn
can land on a worker that never heard of them. Nothing errors. Raise `--workers` only
after `CONVERSATION_STORAGE=backend_api`.

---

## 5. Decisions your team still owns

None of these are code defects. Each is a judgement call deliberately not made on your
behalf.

### CONFIRM — Sign convention must not be "helpfully" normalised
Your data uses Plaid's rule: **positive means money out**. If Laravel inverts it when
serving JSON — a very natural instinct — income and spending swap places in every
figure the assistant reports (savings rate, cash flow, all of it), with no error
anywhere and nothing wrong-looking on screen.

Verified against the dump: known purchases are 18,300 positive to 167 negative; known
income 6 positive to 7,557 negative. The contract checker fails on this. If you do
normalise, say so and one constant (`OUTFLOW_IS_POSITIVE` in
`app/utils/transactions.py`) gets flipped.

### CONFIRM — The `is_review_transfer` rule must match on both sides
This service now excludes a flagged row **only when its category is `TRANSFER_IN` or
`TRANSFER_OUT`**. If Laravel applies the old blanket rule, the two services will
disagree on every total. Evidence and reasoning in `docs/demo-database.md`.

### NICE TO HAVE — Send `subcategory` for precise restaurant answers
Plaid's detailed tier (`FOOD_AND_DRINK_RESTAURANT`) is in your database but discarded
before serving — `display_name` collapses all seven `FOOD_AND_DRINK` rows to one label.
Without it, "how much on restaurants?" returns all food & drink, correct and clearly
labelled as widened, just less precise. Merchant matching has no such dependency.

### CLEAN UP — Test data is sitting in production
Users **231, 240, 267 and 293** have manual transactions named `test`, including a
±$500,000 pair on 2026-08-09, plus roughly nine test user accounts whose names and
emails are obviously non-production (redacted here -- query
`application_users WHERE Name LIKE '%test%' OR email LIKE '%test%'` to list them).

These were **not** filtered in code. All ten live `test` rows are manual entries
(`is_plaid=0`), and manual entries are legitimate — this codebase already had a bug
where they were dropped, understating one user's July by exactly $500. Filtering on
`is_plaid=0` would re-break that; filtering on the literal name `"test"` would suppress
real rows by string match. It is a data cleanup in the MyFi database.

---

## 5a. Diagnosing a failed data read

When the assistant says *"your figures are temporarily unavailable"*, the AI service
reached Laravel and the call failed. It is working as designed — it refuses to invent
numbers. The logs name the cause, with the structured fields included in both JSON and
plain format:

| Log line | Cause |
|---|---|
| `backend_request_error` | Could not connect — wrong `BACKEND_API_BASE_URL`, DNS, port, or firewall |
| `backend_request_failed` + `status=404` | Endpoint not implemented yet |
| `backend_request_failed` + `status=401`/`403` | `BACKEND_API_SERVICE_TOKEN` wrong or empty |
| `backend_request_timeout` | Laravel too slow — raise `BACKEND_API_TIMEOUT_SECONDS` |

Example: `... ERROR app.repositories.base: backend_request_failed | path=/internal/transactions status=401`

The response `metadata` is also diagnostic: `data_used: false` with `tools_used: []`
means no data was read, while a correct `intent`, `period_start` and `period_end`
confirm planning and date resolution worked and only the fetch failed.

---

## 6. The one thing not to get wrong

**Do not deploy expecting it to work before the endpoints exist.**

- With `BACKEND_INTEGRATION_MODE=backend_api` and no endpoints → every question errors.
- With `BACKEND_INTEGRATION_MODE=sqlite` → production startup **refuses to boot**, by
  design, because that mode serves a static snapshot rather than live data.

The AI service is finished. It is waiting on Laravel.

---

## Reference

| What | Where |
|---|---|
| Endpoint contracts | `docs/handover-backend.md` §5 |
| Conversation storage contract | `docs/handover-backend.md` §8 |
| Mobile app proxy (PHP) | `docs/handover-backend.md` §9 |
| Contract checker | `scripts/verify_backend_contract.py` |
| Sign convention | `app/utils/transactions.py` |
| Category / merchant matching | `app/utils/categories.py`, `app/utils/merchants.py` |
| Transfer-flag reasoning | `docs/demo-database.md` |

Verified on the real dump: 403 tests passing, ruff clean, mypy clean across 71 files.
