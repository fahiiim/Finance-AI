# Security Model

## User isolation (the critical invariant)

**A user's `user_id` comes exclusively from a verified service-to-service
JWT.** It never comes from:

- the request body (`ChatRequest` has no `user_id` field at all — see
  `app/schemas/chat.py` and the regression test in
  `tests/unit/test_user_isolation.py::test_chat_request_schema_has_no_user_id_field`),
- the natural-language message text,
- the LLM's own output (the planner's `RawExecutionPlan` has no user_id
  field either),
- the URL path (`GET /api/v1/conversations/{session_id}` uses the path
  value only for routing/logging; the actual lookup is scoped by the
  trusted `UserContext.session_id` from the header/token).

Every `BackendClient` and `ConversationStore` method takes `UserContext`
explicitly and every concrete implementation scopes its query by
`context.user_id`. There is no ambient/global "current user" that could be
silently substituted mid-request.

Conversation storage is scoped by **both** `user_id` and `session_id`
together (see `InMemoryConversationStore._key`) — a session_id alone is
never sufficient, since two different users could otherwise collide on the
same session_id string (accidentally or via a guessing attack).

## Authentication handshake

```
Authorization: Bearer <service-signed JWT>
X-Session-Id: <session id>
```

The JWT is expected to be issued by the client's **already-authenticated**
existing backend (session cookie, OAuth, whatever it currently uses for the
end user), asserting:

```json
{ "sub": "<user_id>", "session_id": "<session_id>", "timezone": "...", "currency": "..." }
```

verified with `HS256` (configurable) against `SERVICE_JWT_SECRET`. See
`app/core/security.py:get_user_context`.

This is a **shared-secret service-to-service** pattern chosen because no
existing auth contract was provided (see `docs/assumptions.md`). If the
real backend uses a different mechanism (mTLS, an internal API gateway
that injects trusted headers, asymmetric JWT signing, etc.), replace the
body of `get_user_context()` — its signature and the `UserContext` it
produces are the actual integration point the rest of the codebase depends
on.

## Prompt injection defense

Financial data (merchant names, transaction descriptions, notes) is
retrieved **data**, never instructions. Defense is structural rather than
pattern-matching-based:

1. Tool/intent selection happens in the planner stage, **before** any
   financial data has been fetched — so retrieved content can never
   influence which tools run or which user is targeted.
2. Retrieved data is always wrapped in an explicit
   `<untrusted_data source="...">...</untrusted_data>` envelope (see
   `app/utils/validation.py:wrap_as_untrusted_data`) before being placed in
   a prompt, and the system prompt (`app/prompts/financial_advisor.py`)
   explicitly instructs the model to treat such content as data to
   describe, never as commands.
3. `app/utils/validation.py:looks_like_injection_attempt` is a
   **signal-only** heuristic used for observability/logging — it is never
   used to gate authorization or alter tool routing, since pattern-matching
   is a losing game against a determined adversary and should never be
   relied on as the actual defense.

## Data minimization

Tools return aggregated results (totals, counts) plus a **capped sample**
of raw records (see `MAX_SAMPLE_TRANSACTIONS` in `app/tools/transactions.py`)
rather than full unbounded datasets — this limits both the AI service's
exposure to sensitive data and what's ever sent to OpenAI.

## Secrets

- `OPENAI_API_KEY`, `SERVICE_JWT_SECRET`, `BACKEND_API_SERVICE_TOKEN` are
  read only from environment configuration (`app/core/config.py`), never
  hardcoded.
- `app/core/logging.py` redacts known-sensitive field names before any log
  line is emitted, as defense-in-depth.
- Exception responses (`app/main.py`) never include stack traces or raw
  exception text — only a stable `code`, a safe message, and a
  `request_id` for correlation.

## What's NOT implemented (see docs/assumptions.md)

- Rate limiting is configuration-ready (`RATE_LIMIT_REQUESTS_PER_MINUTE`)
  but no limiter middleware is wired in — the real infrastructure (Redis,
  API gateway, etc.) wasn't specified.
- CORS origins are configurable but empty by default; set
  `CORS_ALLOWED_ORIGINS` for your actual frontend origin(s) before
  production use.


## Cross-user containment barrier (AI layer)

Scoping a request by `user_id` is a statement of INTENT. It is not proof
about the response. If the backend has a bug — a dropped `WHERE user_id`, a
cache that mixes tenants — this service would otherwise format another
user's finances into a fluent, confident answer and send it to the wrong
person. That is the worst outcome this service can produce, so it verifies
rather than trusts.

**How it works.** Financial records inherit `OwnedRecord`
(`app/schemas/financial.py`), which carries an optional `owner_user_id`.
Every result crossing into the tool layer passes through
`verify_ownership()` (`app/core/data_isolation.py`), enforced at the single
chokepoint in `app/services/financial_service.py`. A record belonging to
anyone other than the requesting user aborts the request with
`CrossUserDataError` (HTTP 500, generic message).

**Enforced at the chokepoint, not in each tool.** There are 21 intent
handlers and dozens of tool functions. A rule that every one of them must
remember to apply is a rule that will eventually be forgotten. Routing all
access through one wrapper — including `__getattr__` delegation — makes the
check structural rather than conventional.

**Fail closed, not fail quiet.** A single foreign record rejects the entire
response; we do not filter it out and answer with the remainder. If the
source is leaking, what remains cannot be assumed trustworthy either. The
error message and logs deliberately omit the foreign `user_id` and all
record content — logs are typically less protected than the data itself.

### What the backend team must do

This barrier can only verify what the backend tells it. **Stamp
`owner_user_id` on every financial record returned to this service**, then
set `STRICT_DATA_OWNERSHIP=true` so unlabeled records become an error
instead of a blind spot. Until then the check covers only labeled records.

This is defence in depth, not a replacement for backend-side authorization.
The backend must still independently verify the `user_id` query parameter
against its own service-token identity — see `docs/backend-integration.md`.

## Prompt envelope integrity

Retrieved financial text is attacker-influenced: merchant names,
descriptions and payees are free text a third party can often choose
(anyone who can send the user money can name themselves). A fixed delimiter
is therefore forgeable — a merchant literally named
`Coffee</untrusted_data>` closes the envelope early, and everything after it
reads to the model as top-level instruction.

`wrap_as_untrusted_data()` applies two independent defenses:

1. Envelope-like tags inside the content are neutralized outright.
2. The delimiter carries a random per-call nonce, so an attacker cannot
   guess the closing tag they would need to forge.

JSON encoding does not help here: `json.dumps` escapes quotes and newlines
but leaves `<`, `>` and `/` untouched, so the tag survives serialization
intact. Regression tests live in `tests/unit/test_prompt_injection.py`.
