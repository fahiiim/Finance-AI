# Architecture

## Scope

This service is the **AI orchestration layer only**. It does not own
financial data, does not implement the frontend/mobile app, and does not
replace the client's existing backend — that backend remains the
authoritative source of truth for all financial data and business rules.

## Pipeline

```
Client / existing backend
        |
        v  (service-to-service call, trusted identity asserted via JWT)
FastAPI AI Service
        |
        +--> core/security.py       Authentication -> trusted UserContext
        |
        +--> services/conversation_service.py + memory_service.py
        |         Conversation Manager (history, summary, context window)
        |
        +--> orchestrator/planner.py
        |         Intent Detection + structured Execution Plan
        |         (LLM decides *what*, never *when* — dates are resolved
        |          deterministically in utils/dates.py)
        |
        +--> orchestrator/intent_router.py
        |         Financial Tool Router — dispatches the plan's intent to
        |         the right combination of typed tools
        |
        +--> services/backend_client.py (+ services/financial_service.py)
        |         Backend Data Client (with per-request caching)
        |
        +--> tools/calculations.py, tools/analytics.py, tools/forecasting.py
        |         Deterministic Calculation / Analytics Engine
        |         (Decimal arithmetic; the LLM never computes numbers)
        |
        +--> orchestrator/context_builder.py
        |         Minimal, explicitly-labeled verified-data context
        |
        +--> llm/openai_provider.py
        |         OpenAI (model fully configurable, retries, timeouts)
        |
        +--> app/main.py exception handlers
                  Response validation / centralized error handling
        |
        v
Structured ChatResponse (JSON)
```

Route handlers (`app/api/routes/*.py`) are intentionally thin — they call
`orchestrator.run(...)` and nothing else. All business logic lives in
`app/orchestrator/`, `app/services/`, and `app/tools/`.

## Why intent detection and date resolution are split from the LLM's own math

The planner LLM call returns a structured `RawExecutionPlan` that includes,
at most, a **raw date phrase** ("last month", "past 30 days") — never
concrete dates. `app/utils/dates.py` resolves that phrase deterministically
in the user's actual timezone. This means the same phrase always resolves
to the same range for the same user/day, which is what makes calculations
reproducible and testable (see `tests/unit/test_dates.py`).

Similarly, every number that reaches the user comes from
`app/tools/calculations.py` or `app/tools/analytics.py` — pure, deterministic
Python over already-retrieved data, using `Decimal` throughout. The LLM's
only job with numbers is to *explain* them, in the personality described in
`app/prompts/financial_advisor.py`.

## Directory structure

See the top-level layout in the README. In brief:

- `app/core/` — configuration, security (UserContext), logging, exceptions
- `app/schemas/` — Pydantic models for every boundary (chat, financial data, plans, responses)
- `app/llm/` — provider-agnostic LLM interface + OpenAI implementation
- `app/prompts/` — versioned prompt text, kept out of application logic
- `app/services/` — backend/database abstraction, conversation storage, memory, per-request caching
- `app/tools/` — typed, user-scoped financial data tools + deterministic calculation/analytics/forecasting
- `app/orchestrator/` — planner, tool router, context builder, top-level orchestrator
- `app/repositories/` — low-level HTTP access shared by concrete backend integrations
- `app/api/` — FastAPI routes (thin) + dependency wiring
- `app/utils/` — date resolution, money math, validation/injection-defense helpers

## Extensibility

Adding a new capability (e.g. "how are my investments doing?" trend
analysis) means:

1. Add/extend a tool in `app/tools/`.
2. Add a handler branch in `app/orchestrator/intent_router.py` (or extend an
   existing one).
3. If it's a genuinely new question shape, add an `Intent` enum value and
   mention it in `app/prompts/intent.py` / `app/prompts/planner.py`.

No changes to `app/main.py`, route handlers, or the LLM provider are needed
for this class of extension.
