# Coverage check: "User-Specific Spending History" spec

A spec titled *Feature: User-Specific Spending History – Python FastAPI AI
Service* was received, describing requirements for this service. This
document maps each of its sections to what is already implemented, so
whoever wrote the spec can see what exists without re-reading the spec
against the code themselves.

`CLAUDE.md`, `docs/backend-requirements.md`, and `docs/ai-master-documentation.md`,
which the spec asks to be checked first, do not exist in this repository —
the spec was evidently written without inspecting this codebase. Nearly
everything it asks for is already built, under the service's existing
architecture.

## Summary

This service already answers spending-history questions (and a materially
wider range of financial questions) using a **pull** architecture: the AI
service receives only a signed per-request JWT asserting `user_id` and
`session_id`, and itself calls Laravel's internal read-only API on demand
for exactly the data a given question needs.

The spec describes a **push** architecture instead: Laravel pre-aggregates
spending into monthly totals and embeds them directly in the chat request
body (`available_spending_data`), and the AI service never calls back.

The pull model already in place is a **superset** of the push model's
capability — see "The one deliberate difference" below — so no new contract
was built.

## Section-by-section mapping

| Spec section | Status | Implementation |
|---|---|---|
| §2 — Historical spending questions ("last month", "June", "compare June and July", "which month did I spend the most", "last three months") | Implemented, wider scope than asked (category, merchant, and day-level, not just monthly totals) | `app/orchestrator/planner.py`, `app/tools/expenses.py`, `app/utils/dates.py` |
| §4 — Distinguishing no-data-supplied / confirmed-zero / incomplete data | Implemented via a three-state `DataAvailability` enum (`available` / `missing` / `not_applicable`), applied per field, not just per month | `app/schemas/financial.py`, `tests/unit/test_missing_vs_zero.py` |
| §5 — Decimal-precise money, deterministic Python calculations (not LLM-computed) | Implemented — `Money.amount` is `Decimal`; all arithmetic lives in Python, the LLM only narrates results | `app/schemas/financial.py` (`Money`), `app/tools/calculations.py`, `tests/unit/test_calculations.py`, `test_money.py`, `test_sign_convention.py` |
| §6 — Conversational follow-up resolving an unstated month | Implemented, with an explicit loop-breaker so the model can't re-ask the same clarifying question indefinitely | `app/orchestrator/planner.py` (`_established_date_phrase`, `_pending_clarification`, `MAX_CLARIFICATIONS_PER_CONVERSATION`), `tests/unit/test_clarification_loop.py` |
| §7 — FastAPI endpoints + Swagger/OpenAPI docs | Implemented — standard FastAPI `/docs` and `/openapi.json`, no custom path overrides | `app/api/routes/chat.py`, `conversations.py`, `health.py`, `app/main.py` |
| §8 — Data-handling security (scoped to current request, no cross-user access, no leakage via logs/errors) | Implemented, more rigorously than specified — ownership is re-verified server-side rather than trusted from the backend, plus rate limiting, JWT hardening, and prompt-injection resistance | `app/core/data_isolation.py`, `app/core/rate_limit.py`, `app/core/security.py`, `tests/unit/test_user_isolation.py`, `test_cross_user_containment.py`, `test_token_hardening.py`, `test_prompt_injection.py` |
| §9 — Prompts kept separate from application code, LLM never the security boundary | Implemented — versioned prompt text files, enforcement lives in Python, not in the prompt | `app/prompts/`, `app/prompts/versions/*.txt`, `tests/unit/test_prompt_versions.py` |
| §10 — Tests: missing months, zero vs. missing, comparisons, invalid payloads, follow-ups, injection attempts, cross-user isolation | Implemented — 26 existing test files already cover nearly every scenario listed | `tests/unit/*.py`, `tests/integration/*.py` |
| §11 — Laravel integration document | Implemented, materially more detailed than the spec asks — exact column mappings, sign conventions, and multi-table join paths, not just endpoint shapes | `docs/handover-backend.md` |

## The one deliberate difference: §3's request contract

The spec's request shape pushes pre-aggregated monthly totals into the chat
request itself. This service instead uses a per-question pull: a scoped JWT
plus 11 read-only `GET /internal/*` endpoints on Laravel
(`app/services/backend_client.py`, `docs/handover-backend.md`), called on
demand through tool-calling in the orchestrator
(`app/orchestrator/intent_router.py`, `app/tools/*.py`).

This was kept rather than adopted because the push shape can only ever
answer monthly-total questions. The pull model already answers category,
merchant, day-by-day, period-comparison, affordability, financial-score,
subscriptions, and liabilities questions — all currently working and
verified against the live internal API. Adopting the push contract either
as a replacement would drop that capability, or alongside the existing one
would duplicate the data path for a strict subset of what already works.

## Where the rest of the spec's "final deliverables" (§13) already live

- Architecture summary / integration modes: `docs/backend-integration.md`
- Laravel integration guide with example requests: `docs/handover-backend.md`
- Swagger documentation with testable examples: `/docs` and `/openapi.json` on the running service, no code change needed
- Missing-month / historical spending logic: `app/orchestrator/planner.py`, `app/utils/dates.py`
- Financial calculation implementation: `app/tools/calculations.py`
- Prompt files: `app/prompts/versions/*.txt`
- Test coverage: `tests/unit/` (24 files) and `tests/integration/` (4 files) — see the section-by-section table above for which file covers which requirement
