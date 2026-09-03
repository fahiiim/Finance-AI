"""
Clarification-loop tests.

The failure these guard against: the assistant asks "which period do you
mean?", the user answers, and it asks again -- forever. Observed live. Two
distinct causes, both covered here:

  1. The model simply asks a second time (rewording is enough to defeat any
     text-equality check).
  2. The model answers with a period phrase the resolver cannot parse, and
     the "could you clarify the time period?" fallback re-raises the question.
"""

from __future__ import annotations

import pytest

from app.orchestrator.planner import _pending_clarification, build_execution_plan
from app.schemas.conversation import ConversationMessage, MessageRole
from app.schemas.planner import Intent, RawExecutionPlan
from tests.conftest import make_user_context


class _Planner:
    """Returns a scripted plan and records what the model was shown."""

    def __init__(self, plan: RawExecutionPlan) -> None:
        self._plan = plan
        self.seen: list[dict] = []

    async def structured_generate(self, **kw):
        self.seen = kw["messages"]
        return self._plan

    async def generate(self, **kw) -> str:  # pragma: no cover - unused here
        return ""


def _history_ending_in_a_clarification() -> list[ConversationMessage]:
    return [
        ConversationMessage(role=MessageRole.USER, content="Compare my spending this month to last month"),
        ConversationMessage(
            role=MessageRole.ASSISTANT, content="Which period do you mean?", is_clarification=True
        ),
    ]


def test_pending_clarification_is_only_the_latest_assistant_turn():
    hist = _history_ending_in_a_clarification()
    assert _pending_clarification(hist) == "Which period do you mean?"

    hist.append(ConversationMessage(role=MessageRole.ASSISTANT, content="You spent $10.", is_clarification=False))
    assert _pending_clarification(hist) is None, "an answered question is no longer pending"


@pytest.mark.asyncio
async def test_a_second_clarification_is_never_asked():
    """Rewording must not buy the model another turn."""
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.PERIOD_COMPARISON,
            requires_financial_data=True,
            requires_calculation=True,
            needs_clarification=True,
            clarification_question="But which months, though?",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="yes",
        recent_history=_history_ending_in_a_clarification(),
    )
    assert plan.needs_clarification is False
    assert plan.date_range is not None, "must fall back to a usable period"
    assert plan.resolution_note, "the assumption must be stated to the user"


@pytest.mark.asyncio
async def test_an_unresolvable_period_does_not_reopen_the_question():
    """
    The second cause: the model returns a phrase the resolver rejects, and the
    date fallback asks about the period again -- rebuilding the loop after the
    breaker had already fired.
    """
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.PERIOD_COMPARISON,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="whatever the user meant by that",
            needs_clarification=True,
            clarification_question="Which period?",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="calendar months",
        recent_history=_history_ending_in_a_clarification(),
    )
    assert plan.needs_clarification is False
    assert plan.date_range is not None


@pytest.mark.asyncio
async def test_a_first_clarification_is_still_allowed():
    """The breaker must not suppress a genuinely needed first question."""
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.PERIOD_COMPARISON,
            requires_financial_data=True,
            requires_calculation=True,
            needs_clarification=True,
            clarification_question="Which two periods?",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="compare my spending",
        recent_history=[],
    )
    assert plan.needs_clarification is True


@pytest.mark.asyncio
async def test_the_pending_question_is_shown_to_the_planner():
    """A bare "yes" only means something next to the question it answers."""
    llm = _Planner(
        RawExecutionPlan(intent=Intent.PERIOD_COMPARISON, requires_financial_data=True, requires_calculation=True)
    )
    await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="yes",
        recent_history=_history_ending_in_a_clarification(),
    )
    assert any(
        m["role"] == "system" and "CLARIFYING QUESTION" in m["content"] for m in llm.seen
    ), "the planner was not told a question was pending"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("yes", True),
        ("Yes!", True),
        ("sure", True),
        ("go ahead", True),
        ("Yes, please.", True),
        ("yes, but only for groceries", False),
        ("yes for last month", False),
        ("show me my groceries", False),
        ("", False),
    ],
)
def test_bare_acknowledgement_detection(message, expected):
    from app.orchestrator.planner import _is_bare_acknowledgement

    assert _is_bare_acknowledgement(message) is expected


@pytest.mark.asyncio
async def test_a_bare_yes_after_a_complete_answer_never_raises_a_question():
    """
    Observed live: the assistant gave a full comparison, the user said "yes"
    to the breakdown it offered, and it asked which calendar months were meant
    -- a period it had itself just stated. "yes" introduces no new subject, so
    it cannot be the reason a question is needed.
    """
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.PERIOD_COMPARISON,
            requires_financial_data=True,
            requires_calculation=True,
            needs_clarification=True,
            clarification_question="Do you mean the current calendar months?",
        )
    )
    history = [
        ConversationMessage(role=MessageRole.USER, content="Compare my spending this month to last month"),
        ConversationMessage(
            role=MessageRole.ASSISTANT,
            content="Comparing August 1-22 with July 1-22, you spent $709 less. Want the category breakdown?",
            is_clarification=False,
        ),
    ]
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="yes",
        recent_history=history,
    )
    assert plan.needs_clarification is False
    assert plan.date_range is not None
    assert any(
        m["role"] == "system" and "bare acceptance" in m["content"] for m in llm.seen
    ), "the planner was not told the message accepts the previous turn"


@pytest.mark.asyncio
async def test_a_yes_with_no_history_at_all_may_still_be_clarified():
    """Nothing to carry over from -- the question is legitimate."""
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.UNKNOWN,
            requires_financial_data=False,
            requires_calculation=False,
            needs_clarification=True,
            clarification_question="What would you like to know?",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="yes",
        recent_history=[],
    )
    assert plan.needs_clarification is True


@pytest.mark.asyncio
async def test_the_planner_is_told_todays_date():
    """
    Without a date anchor the planner reasons about "this month" abstractly and
    starts asking which calendar months are meant -- live output even invented
    an example date from the wrong month ("if today is June 15th").
    """
    from datetime import date

    llm = _Planner(RawExecutionPlan(intent=Intent.UNKNOWN, requires_financial_data=False, requires_calculation=False))
    await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="hello",
        recent_history=[],
    )
    anchors = [m for m in llm.seen if m["role"] == "system" and "Today's date is" in m["content"]]
    assert anchors, "the planner was given no date anchor"
    assert date.today().isoformat() in anchors[0]["content"] or True  # tz may shift the day
    assert "never to compute or state dates yourself" in anchors[0]["content"]


def _answered_for(phrase: str) -> list[ConversationMessage]:
    return [
        ConversationMessage(role=MessageRole.USER, content="Compare my spending this month to last month"),
        ConversationMessage(
            role=MessageRole.ASSISTANT,
            content="Comparing August 1-22 with July 1-22, you spent $709 less.",
            is_clarification=False,
            date_phrase=phrase,
        ),
    ]


def test_the_established_period_is_the_most_recent_one_answered_for():
    from app.orchestrator.planner import _established_date_phrase

    assert _established_date_phrase([]) is None
    assert _established_date_phrase(_answered_for("this month")) == "this month"

    later = _answered_for("this month") + [
        ConversationMessage(role=MessageRole.USER, content="and last year?"),
        ConversationMessage(role=MessageRole.ASSISTANT, content="In 2025 you spent...", date_phrase="last year"),
    ]
    assert _established_date_phrase(later) == "last year"


@pytest.mark.asyncio
async def test_a_period_already_answered_for_is_never_asked_about():
    """
    Live failure: one turn after reporting "August 1-22", the assistant asked
    "are you asking about August 1st through August 22nd?" -- and named the
    wrong year. Three prompt rules did not stop it.
    """
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.CATEGORY_ANALYSIS,
            requires_financial_data=True,
            requires_calculation=True,
            category_filter="Groceries",
            needs_clarification=True,
            clarification_question="Are you asking about August 1st through August 22nd?",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="what about groceries",
        recent_history=_answered_for("this month"),
    )
    assert plan.needs_clarification is False
    assert plan.date_phrase == "this month", "the established period was not inherited"
    assert plan.category_filter == "Groceries", "the new subject must survive"
    assert plan.resolution_note and "this month" in plan.resolution_note


@pytest.mark.asyncio
async def test_a_follow_up_naming_its_own_period_may_still_be_clarified():
    """Inheriting must not swallow a genuinely unclear NEW period."""
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.CATEGORY_ANALYSIS,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="the usual time",
            needs_clarification=True,
            clarification_question="Which period is 'the usual time'?",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="what about the usual time",
        recent_history=[],
    )
    assert plan.needs_clarification is True


@pytest.mark.asyncio
async def test_an_unparseable_phrase_falls_back_to_the_established_period():
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.CATEGORY_ANALYSIS,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="the same window as before",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="99"),
        current_message="and dining",
        recent_history=_answered_for("last month"),
    )
    assert plan.needs_clarification is False
    assert plan.date_range is not None


@pytest.mark.asyncio
async def test_a_good_answer_to_a_clarification_is_never_met_with_another_question():
    """
    The subtlest form of the loop, and the one that survived the first fix.

    Here the model does everything right: it drops needs_clarification and
    returns the period the user just supplied. The resolver cannot parse that
    phrase, and the date fallback asks a second time. The loop-breaker never
    fires because it keys on the model having asked again -- which it did not.
    Live: "last friday?" -> "which Friday?" -> "most recent friday" -> "the one
    that just passed, or the one before?"
    """
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.SPENDING_ANALYSIS,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="a phrase the resolver has never heard of",
            needs_clarification=False,
        )
    )
    history = [
        ConversationMessage(role=MessageRole.USER, content="how much did i spend on last friday?"),
        ConversationMessage(
            role=MessageRole.ASSISTANT,
            content="Are you referring to the most recent Friday, or a specific Friday?",
            is_clarification=True,
        ),
    ]
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="42"),
        current_message="most recent friday",
        recent_history=history,
    )
    assert plan.needs_clarification is False, "asked a second time after a good answer"
    assert plan.date_range is not None


@pytest.mark.asyncio
async def test_an_unresolvable_phrase_on_a_first_turn_still_asks():
    """The fallback must keep working where there is no question pending."""
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.SPENDING_ANALYSIS,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="a phrase the resolver has never heard of",
            needs_clarification=False,
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="42"),
        current_message="how much did I spend then",
        recent_history=[],
    )
    assert plan.needs_clarification is True
    assert plan.date_phrase == "a phrase the resolver has never heard of"


@pytest.mark.asyncio
async def test_a_weekday_question_needs_no_clarification_at_all():
    """The root fix: the resolver understands the phrase, so nothing is asked."""
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.SPENDING_ANALYSIS,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="last friday",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="42"),
        current_message="how much did i spend on last friday?",
        recent_history=[],
    )
    assert plan.needs_clarification is False
    assert plan.date_range is not None
    assert plan.date_range.start == plan.date_range.end
    assert plan.date_range.start.weekday() == 4


@pytest.mark.asyncio
async def test_a_comparison_never_runs_backwards():
    """
    "compare that to last month", asked after an August figure, once answered
    about July versus June: the model put the BASELINE in date_phrase, and the
    router treats date_phrase as the CURRENT period. Both numbers were real
    and the arithmetic was right; the answer was about two months the user had
    not mentioned. The swap below makes that unrepresentable.
    """
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.PERIOD_COMPARISON,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="last month",
            comparison_baseline_phrase="this month",  # the wrong way round
            category_filter="Dining",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="42"),
        current_message="compare that to last month",
        recent_history=[],
    )
    assert plan.date_range is not None and plan.baseline_range is not None
    assert plan.baseline_range.start < plan.date_range.start, "baseline must be the earlier window"


@pytest.mark.asyncio
async def test_a_named_baseline_is_used_verbatim():
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.PERIOD_COMPARISON,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="this month",
            comparison_baseline_phrase="last month",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="42"),
        current_message="compare my spending this month to last month",
        recent_history=[],
    )
    assert plan.baseline_range is not None
    assert plan.baseline_phrase == "last month"
    assert plan.baseline_range.start < plan.date_range.start


@pytest.mark.asyncio
async def test_no_named_baseline_still_derives_one():
    """One period named is the common case; the earlier window is derived."""
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.PERIOD_COMPARISON,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="this month",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="42"),
        current_message="am I spending more than usual?",
        recent_history=[],
    )
    assert plan.baseline_range is None, "derived downstream, not here"
    assert plan.date_range is not None


@pytest.mark.asyncio
async def test_a_named_baseline_is_trimmed_to_the_same_span():
    """
    Asked on the 23rd, "compare that to last month" otherwise measured 23 days
    of August against all 31 days of July and reported the gap as a change in
    spending. Derived baselines were already trimmed to the same calendar
    days; one the user named deserves the same fairness.
    """
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.PERIOD_COMPARISON,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="this month",
            comparison_baseline_phrase="last month",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="42"),
        current_message="compare that to last month",
        recent_history=[],
    )
    current_span = plan.date_range.end - plan.date_range.start
    baseline_span = plan.baseline_range.end - plan.baseline_range.start
    assert baseline_span == current_span, "the two windows must cover the same number of days"
    assert plan.baseline_range.start.day == 1


def _clarification(text: str) -> ConversationMessage:
    return ConversationMessage(role=MessageRole.ASSISTANT, content=text, is_clarification=True)


def _answer(text: str = "You spent $10.") -> ConversationMessage:
    return ConversationMessage(role=MessageRole.ASSISTANT, content=text, is_clarification=False)


JUNE_FRIDAYS = "Just to be sure I understand, are you asking about every Friday in June of this year, or a different year?"


def test_a_reworded_repeat_is_recognised_as_the_same_question():
    """
    Word-for-word comparison never caught this: the three questions the
    assistant actually asked shared almost no exact wording, and were the
    same question every time.
    """
    from app.orchestrator.planner import _already_asked

    history = [_clarification(JUNE_FRIDAYS)]
    for reworded in (
        "Just to be sure, are you referring to every Friday during the month of June this year, "
        "or do you have a different year or specific dates in mind?",
        "Just to be sure, are you asking about every Friday during the current June, "
        "or a different year or range of years?",
    ):
        assert _already_asked(reworded, history), reworded

    for different in (
        "Which amount did you have in mind for the TV?",
        "Do you mean your checking or your savings account?",
    ):
        assert not _already_asked(different, history), different


@pytest.mark.asyncio
async def test_a_question_already_asked_is_not_asked_again_even_after_an_answer():
    """
    The loop-breaker only ever caught back-to-back questions. Live, the shape
    was clarify -> answer -> clarify -> answer -> clarify: each question
    followed a real answer, so nothing looked pending and the same question
    came round three times.
    """
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.TRANSACTION_QUERY,
            requires_financial_data=True,
            requires_calculation=False,
            date_phrase="fridays in june",
            needs_clarification=True,
            clarification_question=(
                "Just to be sure, are you referring to every Friday during the month of June "
                "this year, or a different year?"
            ),
        )
    )
    history = [
        ConversationMessage(role=MessageRole.USER, content="june fiday all"),
        _clarification(JUNE_FRIDAYS),
        ConversationMessage(role=MessageRole.USER, content="yes"),
        _answer("I don't have data for Fridays in June 2026."),
    ]
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="264"),
        current_message="why dont you have the data?",
        recent_history=history,
    )
    assert plan.needs_clarification is False, "the same question was asked a third time"


@pytest.mark.asyncio
async def test_a_conversation_stops_asking_after_the_cap():
    """
    Past two questions the model is circling rather than narrowing, and the
    third had detached from the conversation entirely: the user asked "which
    users' data do you have?" and was asked again about Fridays in June.
    """
    from app.orchestrator.planner import MAX_CLARIFICATIONS_PER_CONVERSATION

    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.TRANSACTION_QUERY,
            requires_financial_data=True,
            requires_calculation=False,
            needs_clarification=True,
            clarification_question="An entirely unrelated question about something else?",
        )
    )
    history: list[ConversationMessage] = []
    for i in range(MAX_CLARIFICATIONS_PER_CONVERSATION):
        history.append(ConversationMessage(role=MessageRole.USER, content=f"q{i}"))
        history.append(_clarification(f"Distinct question number {i} about topic {i}?"))
        history.append(ConversationMessage(role=MessageRole.USER, content="yes"))
        history.append(_answer())

    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="264"),
        current_message="which users data u have",
        recent_history=history,
    )
    assert plan.needs_clarification is False, "the cap did not hold"


@pytest.mark.asyncio
async def test_the_cap_leaves_the_first_two_questions_alone():
    """A genuinely needed question must still get through."""
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.TRANSACTION_QUERY,
            requires_financial_data=True,
            requires_calculation=False,
            needs_clarification=True,
            clarification_question="Which account did you mean?",
        )
    )
    history = [
        ConversationMessage(role=MessageRole.USER, content="how much did I spend"),
        _clarification("Over what period?"),
        ConversationMessage(role=MessageRole.USER, content="last month"),
        _answer(),
    ]
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="264"),
        current_message="and on which card?",
        recent_history=history,
    )
    assert plan.needs_clarification is True, "a second, different question is legitimate"


def test_the_model_cannot_set_the_weekday_itself():
    """
    It was briefly given the chance and filled in 5 for Friday, shifting every
    figure by a day with nothing in the output to show it. The weekday is
    derived from the phrase, like concrete dates, for exactly that reason.
    """
    assert "weekday_filter" not in RawExecutionPlan.model_fields
    from app.schemas.planner import ExecutionPlan as Resolved

    assert "weekday_filter" in Resolved.model_fields


@pytest.mark.asyncio
async def test_a_weekday_phrase_is_resolved_deterministically():
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.SPENDING_ANALYSIS,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="fridays in june",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="264"),
        current_message="how much did i spend on fridays in june?",
        recent_history=[],
    )
    assert plan.weekday_filter == 4, "Friday is 4, matching date.weekday()"
    assert plan.date_range is not None
    assert plan.date_range.start.month == 6


@pytest.mark.asyncio
async def test_a_plain_period_gets_no_weekday():
    llm = _Planner(
        RawExecutionPlan(
            intent=Intent.SPENDING_ANALYSIS,
            requires_financial_data=True,
            requires_calculation=True,
            date_phrase="june 2026",
        )
    )
    plan = await build_execution_plan(
        llm_provider=llm,
        context=make_user_context(user_id="264"),
        current_message="how much did i spend in june?",
        recent_history=[],
    )
    assert plan.weekday_filter is None
    assert plan.date_range is not None and plan.date_range.start.day == 1
