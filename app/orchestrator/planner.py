"""
Planning stage: turns a user message + conversation context into a fully
resolved ExecutionPlan.

Two-step split, deliberately:
  1. LLM structured_generate produces a RawExecutionPlan (intent, tools,
     filters, and a raw date PHRASE — never concrete dates).
  2. This module resolves that phrase deterministically via
     app/utils/dates.py, in the user's actual timezone.

This keeps date arithmetic 100% reproducible regardless of what the model
outputs, per financial-ai-analytics-engineer skill (DATE HANDLING).
"""

from __future__ import annotations

import re

from app.core.security import UserContext
from app.llm.base import LLMProvider
from app.prompts.planner import EXECUTION_PLAN_PROMPT_V2
from app.schemas.conversation import ConversationMessage, MessageRole
from app.schemas.planner import (
    DEFAULT_PERIOD_PHRASE,
    PERIOD_SCOPED_INTENTS,
    ExecutionPlan,
    Intent,
    RawExecutionPlan,
    ResolvedDateRange,
)
from app.utils.dates import current_date_in, resolve_date_phrase, resolve_weekday_in_period

_ACKNOWLEDGEMENTS = {
    "y", "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "k", "correct", "right",
    "please", "please do", "go ahead", "do it", "sounds good", "that works", "great",
    "thanks", "thank you", "yes please", "ok thanks", "show me", "continue",
}


# At most this many clarifying questions in one conversation. The second is
# already a bad sign; a third means the model is circling rather than closing
# in, and no answer the user gives is going to end it.
MAX_CLARIFICATIONS_PER_CONVERSATION = 2

# Words that carry no subject. Two clarifying questions phrased differently
# are still the same question, and comparing them word-for-word never catches
# that -- "are you asking about every Friday in June of this year, or a
# different year?" and "are you referring to every Friday during the month of
# June this year, or a different year?" share almost no exact wording.
_QUESTION_NOISE = frozenset({
    "a", "about", "an", "and", "any", "are", "asking", "be", "by", "can", "could", "did", "do",
    "does", "for", "have", "here", "i", "if", "in", "is", "it", "just", "like", "me", "mean",
    "means", "my", "of", "on", "or", "referring", "say", "should", "so", "specific", "sure",
    "tell", "the", "there", "this", "to", "understand", "want", "was", "were", "what", "when",
    "which", "with", "would", "you", "your",
})


def _question_fingerprint(text: str) -> frozenset[str]:
    """The subject words of a question, with the phrasing stripped out."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return frozenset(w for w in cleaned.split() if w not in _QUESTION_NOISE and len(w) > 2)


def _clarifications_asked(history: list[ConversationMessage]) -> list[str]:
    return [m.content for m in history if m.role is MessageRole.ASSISTANT and m.is_clarification]


def _already_asked(question: str | None, history: list[ConversationMessage]) -> bool:
    """
    True when this question has effectively been asked already.

    The loop-breaker only ever caught back-to-back questions, because it keys
    on the previous turn being a clarification. Live, the shape was
    clarify -> answer -> clarify -> answer -> clarify: each question followed a
    real answer, so nothing looked pending and the same question came round
    three times. By the third it had detached from the conversation entirely --
    the user asked "which users' data do you have?" and was asked again about
    Fridays in June.
    """
    if not question:
        return False

    asked = _question_fingerprint(question)
    if not asked:
        return False

    for previous in _clarifications_asked(history):
        before = _question_fingerprint(previous)
        if not before:
            continue
        overlap = len(asked & before) / len(asked | before)
        if overlap >= 0.5:
            return True
    return False


def _is_bare_acknowledgement(message: str) -> bool:
    """
    True for a message that only signals assent -- "yes", "sure", "go ahead".

    Such a message adds no new subject of its own: it can only mean the thing
    the assistant just offered or just asked about. So it can never be the
    reason a NEW question is needed. Whatever was ambiguous about it was
    already established one turn earlier.
    """
    cleaned = re.sub(r"[^a-z ]", " ", message.strip().lower())
    return " ".join(cleaned.split()) in _ACKNOWLEDGEMENTS


def _last_assistant_turn(history: list[ConversationMessage]) -> ConversationMessage | None:
    for m in reversed(history):
        if m.role is MessageRole.ASSISTANT:
            return m
    return None


def _established_date_phrase(history: list[ConversationMessage]) -> str | None:
    """
    The period this conversation is already talking about, most recent first.

    Once the assistant has answered for a window, that window is established.
    A follow-up naming no period of its own ("what about groceries?") means
    that same window -- so asking the user which period they mean is asking
    them to repeat something the conversation has already settled.
    """
    for m in reversed(history):
        if m.role is MessageRole.ASSISTANT and m.date_phrase:
            return m.date_phrase
    return None


def _pending_clarification(history: list[ConversationMessage]) -> str | None:
    """
    The clarifying question the user is currently answering, if any.

    Only the LAST assistant turn counts: if the assistant has since said
    anything else, the question is no longer open.
    """
    last = _last_assistant_turn(history)
    if last is None or not last.is_clarification:
        return None
    return last.content


def _messages_for_planner(
    history: list[ConversationMessage], current_message: str, timezone: str = "UTC"
) -> list[dict[str, str]]:
    # The planner needs to know what day it is. Without an anchor it reasons
    # about "this month" in the abstract and starts asking the user which
    # calendar months they mean -- observed live, complete with an invented
    # example date from the wrong month. It still never computes dates itself;
    # that remains resolve_date_phrase's job. This is orientation, not
    # arithmetic.
    today = current_date_in(timezone)
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                f"Today's date is {today.isoformat()} ({today.strftime('%A, %B %d, %Y')}) "
                f"in the user's timezone ({timezone}). Use this only to understand what "
                "the user is referring to - never to compute or state dates yourself."
            ),
        }
    ]
    messages += [{"role": m.role.value, "content": m.content} for m in history]

    pending = _pending_clarification(history)
    if pending:
        # Spell out the situation rather than hoping the model infers it from
        # transcript shape alone. A bare "yes" carries no information on its
        # own; it only means something relative to the question it answers.
        messages.append(
            {
                "role": "system",
                "content": (
                    "The previous assistant turn was a CLARIFYING QUESTION: "
                    f'"{pending}". '
                    "The user's next message answers it. Combine that answer with "
                    "the earlier request and produce a complete plan now. Do NOT "
                    "set needs_clarification again for this same point — the user "
                    "has already told you what you asked."
                ),
            }
        )

    elif _is_bare_acknowledgement(current_message) and _last_assistant_turn(history) is not None:
        messages.append(
            {
                "role": "system",
                "content": (
                    "The user's next message is a bare acceptance. It refers to whatever "
                    "the previous assistant turn offered or proposed. Plan that action "
                    "now, reusing the period, category and merchant already established "
                    "in that turn, and record what you carried over in resolution_note. "
                    "Do NOT ask which period or category was meant — both are already "
                    "fixed by the turn the user is agreeing with."
                ),
            }
        )

    established = _established_date_phrase(history)
    if established:
        messages.append(
            {
                "role": "system",
                "content": (
                    f'The period already under discussion is "{established}". If the '
                    "user's message names no period of its own, reuse this one in "
                    "date_phrase. Do not ask which period they mean — it is settled."
                ),
            }
        )

    messages.append({"role": "user", "content": current_message})
    return messages


async def build_execution_plan(
    *,
    llm_provider: LLMProvider,
    context: UserContext,
    current_message: str,
    recent_history: list[ConversationMessage],
) -> ExecutionPlan:
    raw = await llm_provider.structured_generate(
        system_prompt=EXECUTION_PLAN_PROMPT_V2,
        messages=_messages_for_planner(recent_history, current_message, context.timezone),
        response_schema=RawExecutionPlan,
        temperature=0.0,
        max_tokens=400,
    )

    # Loop-breaker: AT MOST ONE clarifying question in a row.
    #
    # Matching on identical wording is not enough — the model happily rewords
    # the same question, and an either/or question ("A or B?") cannot be
    # answered by "yes" at all, so it asks again and the conversation never
    # moves. Once we have asked and the user has replied, we must proceed and
    # SAY what we assumed. A stated assumption the user can correct beats a
    # question they cannot escape.
    #
    # This runs BEFORE date resolution, because it may supply the very
    # date_phrase that resolution needs.
    # A bare "yes" is covered too: it answers whatever the assistant last said,
    # whether that was a question ("which months?") or an offer ("want the
    # breakdown?"). Either way the period and category were fixed one turn
    # earlier, so a question raised in response to it is the model failing to
    # use context it already has -- not a genuine ambiguity.
    established_period = _established_date_phrase(recent_history)
    answering_in_context = (
        bool(_pending_clarification(recent_history))
        or (_is_bare_acknowledgement(current_message) and _last_assistant_turn(recent_history) is not None)
        # A period already answered for cannot become a question later. The
        # model asks anyway -- live output asked "are you asking about August
        # 1st through August 22nd?" one turn after reporting exactly that
        # window, and named the wrong year doing it. Three prompt rules did
        # not stop it; remembering the period does.
        or (established_period is not None and not raw.date_phrase)
        # This exact question has already been put to the user. Asking it
        # again cannot produce new information, whatever they answered.
        or _already_asked(raw.clarification_question, recent_history)
        # And a conversation only gets so many attempts. Past the cap the
        # model is circling, not narrowing.
        or len(_clarifications_asked(recent_history)) >= MAX_CLARIFICATIONS_PER_CONVERSATION
    )
    forced = bool(raw.needs_clarification and answering_in_context)
    if forced:
        update: dict = {
            "needs_clarification": False,
            "clarification_question": None,
            "resolution_note": raw.resolution_note
            or "Carried over the period and category from the previous turn.",
        }
        if not raw.date_phrase and raw.requires_financial_data:
            # Inherit the period under discussion; fall back to the documented
            # default only when the conversation has not established one.
            update["date_phrase"] = established_period or "this month"
            if established_period:
                update["resolution_note"] = (
                    f"Used the same period already under discussion ({established_period})."
                )
        raw = raw.model_copy(update=update)

    # A period-scoped question with no period stated gets the current month,
    # and the answer says so. Previously this reached the tools, which raised
    # ValidationFailedError and returned a raw 422 body to the user for
    # perfectly ordinary questions like "what is my dining budget?".
    if not raw.date_phrase and raw.intent in PERIOD_SCOPED_INTENTS and raw.requires_financial_data:
        raw = raw.model_copy(
            update={
                "date_phrase": DEFAULT_PERIOD_PHRASE,
                "resolution_note": raw.resolution_note
                or f"No period was given, so this covers {DEFAULT_PERIOD_PHRASE}.",
            }
        )

    date_range = None
    weekday_filter: int | None = None
    if raw.date_phrase:
        # "Fridays in June" first: it looks like a period phrase but means a
        # window plus a weekday, and resolve_date_phrase alone returns nothing
        # for it. The weekday is derived here, not asked of the model, so it
        # stays as reproducible as every other date decision.
        recurring = resolve_weekday_in_period(raw.date_phrase, context.timezone)
        if recurring is not None:
            date_range, weekday_filter = recurring

        if date_range is None:
            date_range = resolve_date_phrase(raw.date_phrase, context.timezone)
        if date_range is None and established_period and raw.date_phrase != established_period:
            # An unparseable phrase, but the conversation already knows which
            # window it means. Use it rather than losing the turn to a question.
            date_range = resolve_date_phrase(established_period, context.timezone)

        if date_range is None and answering_in_context:
            # We already asked once and the user replied. Asking again about
            # the period — which is what an unresolvable phrase would do —
            # rebuilds the exact loop the breaker above exists to prevent.
            #
            # The test is answering_in_context, NOT forced. forced additionally
            # requires that the MODEL asked again; this branch fires precisely
            # when it did not. Observed live: "how much did I spend last
            # Friday?" → "which Friday?" → "most recent friday" → the model
            # answered correctly with date_phrase="most recent Friday", the
            # resolver did not understand weekdays, and this fallback asked a
            # second time. The loop-breaker never fired because there was
            # nothing to break — the model had behaved perfectly.
            date_range = resolve_date_phrase("this month", context.timezone)

        if date_range is None:
            # The model referenced a date phrase we can't deterministically
            # resolve. Don't guess — ask, rather than silently picking a
            # window that might not match what the user meant.
            return ExecutionPlan(
                intent=raw.intent,
                requires_financial_data=raw.requires_financial_data,
                requires_calculation=raw.requires_calculation,
                required_tools=[],
                needs_clarification=True,
                clarification_question=(
                    f'Could you clarify the time period you mean by "{raw.date_phrase}"?'
                ),
                date_phrase=raw.date_phrase,
            )

    if raw.intent in (Intent.GENERAL_CONVERSATION, Intent.UNKNOWN):
        raw = raw.model_copy(
            update={"requires_financial_data": False, "requires_calculation": False, "required_tools": []}
        )

    baseline_range = None
    if raw.comparison_baseline_phrase:
        baseline_range = resolve_date_phrase(raw.comparison_baseline_phrase, context.timezone)

    if (
        date_range is not None
        and baseline_range is not None
        and (baseline_range.end - baseline_range.start) > (date_range.end - date_range.start)
    ):
        # Trim a named baseline to the same span as the window being asked
        # about. "compare that to last month", asked on the 23rd, otherwise
        # measures 23 days of August against all 31 days of July and calls the
        # difference a change in spending. Same-calendar-days is the rule
        # already applied to derived baselines (previous_calendar_period);
        # a baseline the user named deserves the same fairness, not less.
        baseline_range = ResolvedDateRange(
            start=baseline_range.start,
            end=baseline_range.start + (date_range.end - date_range.start),
            label=baseline_range.label,
        )

    if date_range is not None and baseline_range is not None and baseline_range.start > date_range.start:
        # The two slots came back the wrong way round. A comparison always
        # reads forward -- the baseline is the earlier window and date_range
        # is the one being asked about. Swapping here means no phrasing the
        # model chooses can invert the result, and the answer names its own
        # windows, so the swap is visible rather than silent.
        date_range, baseline_range = baseline_range, date_range

    return ExecutionPlan.from_raw(raw, date_range, baseline_range, weekday_filter)
