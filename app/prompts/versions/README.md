# Prompt version history

Prompt constants live directly in the sibling modules (`system.py`,
`financial_advisor.py`, `intent.py`, `planner.py`, `response.py`), each
suffixed `_V1`, `_V2`, etc. This directory is where the *previous* full text
of a retired version is archived when a new version replaces it in
production, so a regression can be diffed against exactly what changed.

Convention: when introducing `SOME_PROMPT_V2`, copy the outgoing `_V1` text
into `versions/some_prompt_v1.txt` before removing it from the active
module, and note the change (date, reason, who approved it) in
`docs/prompts.md`.

Archived so far (see `docs/prompts.md` for the reason behind each):

| File | Retired | Replaced by |
|---|---|---|
| `financial_advisor_behavior_v1.txt` | 2026-08-24 | `FINANCIAL_ADVISOR_BEHAVIOR_V2` |
| `response_generation_prompt_v1.txt` | 2026-08-24 | `RESPONSE_GENERATION_PROMPT_V2` |
| `execution_plan_prompt_v1.txt` | 2026-08-24 | `EXECUTION_PLAN_PROMPT_V2` |
| `clarification_prompt_v1.txt` | 2026-08-24 | `CLARIFICATION_PROMPT_V2` |

The `_V1` constants themselves are still defined in the sibling modules (nothing references them) so the diff between versions can be taken in Python as well as against these files.
