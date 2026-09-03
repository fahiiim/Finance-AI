# Prompt Architecture

All prompt text lives in `app/prompts/`, never inline in route handlers,
services, or the orchestrator. Every constant is versioned with a `_V1`
suffix.

| File | Constant | Used by | Purpose |
|---|---|---|---|
| `system.py` | `SYSTEM_IDENTITY_V1` | `response.py` | Base identity: financial buddy, not a licensed advisor |
| `financial_advisor.py` | `FINANCIAL_ADVISOR_BEHAVIOR_V2` | `response.py` | Personality, source-of-truth rules, fact/calculation/estimate/suggestion distinction, missing-data behavior |
| `intent.py` | `INTENT_CATALOG_V1`, `INTENT_CLASSIFICATION_PROMPT_V1` | `planner.py` | The intent taxonomy (also embedded in the planner prompt) |
| `planner.py` | `TOOL_CATALOG_V1`, `EXECUTION_PLAN_PROMPT_V2` | `orchestrator/planner.py` | Structured-output planning: intent, tools, filters, raw date phrase |
| `response.py` | `RESPONSE_GENERATION_PROMPT_V2`, `CLARIFICATION_PROMPT_V2` | `orchestrator/orchestrator.py` | Final answer generation; clarifying-question generation |

## The one estimate the assistant is allowed to make

Every figure in an answer comes from the user's data, with a single, bounded
exception: **the price of an item the user asked about but did not price.**

"Should I buy a 4K OLED TV?" is among the most natural questions to put to a
finance assistant, and answering only "tell me the price" fails it. What a
television costs is knowledge about the world; it is not a claim about this
person's finances. So the planner may fill `estimated_price` and
`estimated_price_basis`, and the answer must label it:

> A 55-inch LG C-series 4K OLED TV typically runs around $1,550 (this is an
> estimate — if you have a different price in mind, let me know). After buying
> it, you'd have about $5,885 left, with no unpaid bills due in the next two
> weeks.

The boundary is enforced by structure, not by good intentions:

| Figure | Source |
|---|---|
| Item price, when the user names an item but no price | **Estimated** by the planner, labelled in the answer |
| Item price, when the user states one | The user — always overrides the estimate |
| Balance, bills, uncommitted funds, what remains | The user's data |
| The verdict | Computed in `_handle_affordability_analysis`, never written by the model |

`amount_in_question` (the user's figure) and `estimated_price` (ours) are
separate fields, which is what lets the answer say honestly which number came
from where. An estimate can never silently become "the amount you said".

The verdict is deterministic and has three values. `tight` exists because
clearing to almost exactly zero is affordable in arithmetic and reckless in
practice, and an assistant that calls both of those "yes" is not worth
trusting:

- `comfortable` — leaves at least a quarter of the uncommitted balance
- `tight` — clears, but leaves less than that
- `not_affordable` — does not clear

For something with no meaningful typical price ("can I afford a wedding?")
the planner leaves the estimate empty, and the answer reports what is
available and asks for a figure.

## Why the assistant stops asking

Three separate guards, because a clarifying question that repeats is worse
than one that never gets asked. All three came from one observed
conversation, where the same question was put to the user three times and by
the third had detached from the conversation entirely — they asked "which
users' data do you have?" and were asked again about Fridays in June.

1. **No two clarifications in a row.** Once a question is pending, the next
   turn must produce a plan.
2. **No question asked twice.** Compared on subject words rather than
   wording, because the three real questions shared almost no exact phrasing
   and were the same question every time. This guard survives an answer in
   between — the shape was clarify → answer → clarify → answer → clarify, so
   nothing ever looked "pending".
3. **A cap of two per conversation.** Past that the model is circling, not
   narrowing, and no answer is going to end it.

The trigger underneath that conversation was a phrase the resolver could not
parse — `fridays in june`. Fixing the guards stops the loop; supporting the
phrase removes the reason for it. Both were done, because the next
unparseable phrase should not reopen the same hole.

## The line between derived and asked

Concrete dates are never taken from the model — a phrase in, a resolved range
out, deterministically. The weekday in "Fridays in June" was briefly an
exception: it was added as a field on `RawExecutionPlan`, and the model
promptly filled it with `5` for Friday. Every figure shifted by a day, and
nothing in the output showed it.

It is now derived from the phrase like everything else, and the field does
not exist on the raw plan for the model to fill. `tests/unit/`
`test_clarification_loop.py` asserts that absence, because the mistake is an
easy one to make again.

## Two LLM calls per chat turn

1. **Planning** (`structured_generate`, temperature 0.0): produces a
   `RawExecutionPlan`. Constrained to JSON schema, then re-validated with
   Pydantic locally (`app/llm/openai_provider.py`) — a malformed or
   partial response can never silently pass through.
2. **Response generation** (`generate`, temperature ~0.3–0.4): produces the
   user-facing natural-language answer, given the conversation window plus
   the verified-data envelope (or a clarifying-question prompt if the plan
   flagged `needs_clarification`).

The model never computes numbers in either call — see
`docs/architecture.md` for why dates and math are handled deterministically
outside the LLM.

## Versioning convention

When behavior needs to change, add `SOME_PROMPT_V2` alongside the existing
`_V1` constant (don't mutate `_V1` in place), archive the retiring version's
full text under `app/prompts/versions/`, and note the change here with a
date and reason. This keeps a diffable history of what the model was
actually told at any point in time — useful when investigating a
regression in output quality or safety behavior.

### Version history

#### 2026-08-24 — V2 of the advisor, planner, clarification and response prompts

Reported from live use: *"ai should not say anything like manage this or let
me know — also every response is kinda similar."*

Two distinct faults, both visible in one sample of seven questions:

1. **It offered actions it cannot perform.** "Would you like help tracking or
   managing these subscriptions?" This service is read-only. Every such offer
   is a promise that can never be kept, and the user has no way to know it was
   empty until they accept it.
2. **Seven questions produced seven identical envelopes.** The same three-beat
   shape and the same closing line — "…just let me know!" — around every
   number. A balance lookup was formatted like a period comparison.

Verifying the fix surfaced a third, more serious fault, unrelated to tone:

3. **Comparisons ran against the wrong periods, silently.** A follow-up —
   "compare that to last month", asked after an August figure — was answered
   about *July versus June*. The plan had one `date_phrase` slot, the model put
   the **baseline** in it, and the router treats that slot as the **current**
   period. Both figures were real and the arithmetic was right; the answer was
   about two months the user never mentioned. In another run the follow-up was
   planned as a plain lookup, and the model then produced the difference by
   subtracting a figure from an earlier turn — arithmetic the application never
   performed, across two windows of different lengths.

Changes:

| Constant | Change |
|---|---|
| `FINANCIAL_ADVISOR_BEHAVIOR_V2` | Adds **WHAT YOU CAN AND CANNOT DO** (read-only; never offer to act; decline and stop), **HOW TO END AN ANSWER** (banned closers named individually), **VARY WITH THE QUESTION** (shape follows the question). Built *from* V1 so every source-of-truth, isolation and injection rule carries over verbatim. |
| `EXECUTION_PLAN_PROMPT_V2` | Adds **ACTION REQUESTS ARE OUT OF SCOPE**. V1 had no notion of a request to *change* something, so "set a budget of $300 for dining" was planned as a budget lookup and returned a raw 422, and "move $200 to savings" produced a clarifying question asking which account it should come from — implying the transfer would happen. |
| `CLARIFICATION_PROMPT_V2` | Backstop for the same fault: never ask *how* to carry out an action the service cannot perform. |
| `RESPONSE_GENERATION_PROMPT_V2` | V1's RESPONSE FORMAT prescribed one shape for every question, and the "optional next step" beat invited the offers above. Replaced with LENGTH guidance that matches the answer to the question. |
| `FINANCIAL_ADVISOR_BEHAVIOR_V2` | Also reconciles the price-estimate rule with the banned closers: the invitation to correct an estimate goes in a **parenthetical inside the sentence** ("at an estimated $1,550 (correct me if yours differs)"), never as a trailing "let me know". The two rules were pulling against each other and the invitation was winning — measured at 2 of 7 answers before the fix, 0 after. |
| `FINANCIAL_ADVISOR_BEHAVIOR_V2` | Also adds **NEVER DO THE ARITHMETIC YOURSELF** — no differences, percentages or averages worked out in prose, including against a figure from an earlier turn. |
| `EXECUTION_PLAN_PROMPT_V2` | Also adds the two-slot comparison rule: `date_phrase` is the period asked about, `comparison_baseline_phrase` is what it is compared against. |
| `EXECUTION_PLAN_PROMPT_V2` | Also drops the rule that forced a clarification when an affordability question named no price. The tool now answers without one — it reports uncommitted balance and asks the price in the same turn — so the clarification cost a round-trip and, when the user answered it without a figure, produced a raw 422 in the chat. |

Prompt rules alone were not trusted for the comparison fault — the model had
already ignored three related rules. The schema now carries two explicit
period slots (`comparison_baseline_phrase` on the raw plan, `baseline_range`
on the resolved plan), and `build_execution_plan` **swaps them if they arrive
reversed** and **trims a named baseline to the same span** as the window being
asked about. No phrasing the model chooses can invert a comparison or measure
23 days of one month against 31 of another.

Approved by the product owner in the same message that reported it. Retired
V1 text is archived under `app/prompts/versions/`.

`tests/unit/test_prompt_versions.py` enforces both halves of this: that V2
still contains every V1 guarantee, and that the application actually
references V2 — a reviewed prompt nothing points at is not in production.

**Current state:** `SYSTEM_IDENTITY_V1`, `INTENT_CATALOG_V1`,
`INTENT_CLASSIFICATION_PROMPT_V1` and `TOOL_CATALOG_V1` remain at V1. The
four constants above are at V2.
