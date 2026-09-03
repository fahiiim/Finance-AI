"""
Context builder.

Takes tool results (already aggregated/capped by the tools themselves — see
spec section 28, context minimization) and wraps them as an explicit
untrusted-data envelope for the response-generation prompt, alongside the
resolved conversation history and summary. This is the last stop before the
LLM — nothing raw or unbounded reaches this point.
"""

from __future__ import annotations

import json

from app.schemas.conversation import ConversationMessage, ConversationSummary
from app.schemas.planner import ExecutionPlan
from app.utils.validation import wrap_as_untrusted_data


def build_data_block(tool_results: dict, plan: ExecutionPlan) -> str | None:
    if not tool_results:
        return None
    payload = {"intent": plan.intent.value, "data": tool_results}
    return wrap_as_untrusted_data("verified_financial_data", json.dumps(payload, default=str))


def build_response_messages(
    *,
    recent_history: list[ConversationMessage],
    summary: ConversationSummary | None,
    current_message: str,
    data_block: str | None,
    plan: ExecutionPlan,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []

    if summary is not None:
        messages.append(
            {
                "role": "system",
                "content": f"Summary of earlier conversation: {summary.summary_text}",
            }
        )

    for m in recent_history:
        messages.append({"role": m.role.value, "content": m.content})

    if plan.resolution_note and plan.requires_financial_data:
        # The plan filled a gap the user did not state (a follow-up, or an
        # assumption made after one clarification). Say so in the answer —
        # but only where data was actually fetched: the note discloses which
        # window or category was assumed, and a reply that reads no data has
        # no such assumption to confess. Without the guard, declining an
        # action opened with "Assuming you're asking to cancel your Netflix
        # subscription, ...", which is neither an assumption nor useful.
        # a silent guess about which period was meant is how a confident,
        # wrong number reaches the user.
        messages.append(
            {
                "role": "system",
                "content": (
                    "This request was resolved using context rather than an explicit "
                    f"statement from the user: {plan.resolution_note} "
                    "Make that assumption visible in your answer so the user can "
                    "correct it if it is wrong — naming the period alongside the "
                    "figure is usually enough. Do NOT use a fixed opening formula "
                    "for this, and do not begin consecutive answers the same way; "
                    "a disclosure that becomes a template stops being read. Take "
                    "any dates you state from the data block, never from this note."
                ),
            }
        )

    user_content = current_message
    if data_block:
        user_content = f"{current_message}\n\n{data_block}"
    elif plan.requires_financial_data:
        # Built through the same wrapper as real data so the envelope shape the
        # model is told to trust is identical in both cases.
        user_content = f"{current_message}\n\n" + wrap_as_untrusted_data(
            "verified_financial_data", "No data was returned for this request."
        )

    messages.append({"role": "user", "content": user_content})
    return messages
