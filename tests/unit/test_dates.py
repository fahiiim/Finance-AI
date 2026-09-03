from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.schemas.planner import ResolvedDateRange
from app.utils.dates import (
    current_date_in,
    previous_calendar_period,
    previous_period_of_same_length,
    resolve_date_phrase,
)


def test_today():
    r = resolve_date_phrase("today", "UTC")
    assert r.start == r.end == date.today()


def test_this_month_ends_today():
    r = resolve_date_phrase("this month", "UTC")
    today = date.today()
    assert r.start == date(today.year, today.month, 1)
    assert r.end == today


def test_last_month_full_range():
    today = date.today()
    r = resolve_date_phrase("last month", "UTC")
    first_of_this_month = date(today.year, today.month, 1)
    expected_end = first_of_this_month - timedelta(days=1)
    assert r.end == expected_end
    assert r.start.month == expected_end.month
    assert r.start.day == 1


def test_past_n_days():
    r = resolve_date_phrase("past 30 days", "UTC")
    today = date.today()
    assert r.end == today
    assert (r.end - r.start).days == 29


def test_unrecognized_phrase_returns_none():
    assert resolve_date_phrase("sometime around spring break", "UTC") is None


def test_unknown_timezone_falls_back_to_utc_without_raising():
    r = resolve_date_phrase("today", "Not/ARealZone")
    assert r is not None


def test_previous_period_of_same_length_is_contiguous_and_equal_length():
    from app.schemas.planner import ResolvedDateRange

    current = ResolvedDateRange(start=date(2026, 8, 1), end=date(2026, 8, 15), label="test")
    previous = previous_period_of_same_length(current)
    assert previous.end == current.start - timedelta(days=1)
    assert (previous.end - previous.start) == (current.end - current.start)


# --- phrase canonicalisation -------------------------------------------------
#
# Regression: resolve_date_phrase matched exact strings only. The planner is an
# LLM and says "current calendar month" or "last 30 days" just as readily as
# "this month". Every variant returned None, which surfaced to the user as a
# clarifying question instead of an answer -- and, after a clarification, as an
# unbreakable loop.

@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("this month", "this_month"),
        ("This Month", "this_month"),
        ("current month", "this_month"),
        ("current calendar month", "this_month"),
        ("the current calendar month", "this_month"),
        ("this month so far", "this_month"),
        ("month to date", "this_month"),
        ("MTD", "this_month"),
        ("last month", "last_month"),
        ("previous month", "last_month"),
        ("prior calendar month", "last_month"),
        ("last 30 days", "past_30_days"),
        ("past 30 days", "past_30_days"),
        ("the last 7 days", "past_7_days"),
        ("year to date", "this_year"),
        ("YTD", "this_year"),
        ("previous quarter", "last_quarter"),
    ],
)
def test_phrase_variants_resolve(phrase, expected):
    r = resolve_date_phrase(phrase, "UTC")
    assert r is not None, f"{phrase!r} did not resolve"
    assert r.label == expected


@pytest.mark.parametrize(
    "phrase",
    [
        "this month vs last month",
        "this month versus last month",
        "the current calendar month compared to the previous calendar month",
        "this month compared with last month",
    ],
)
def test_comparison_phrases_resolve_to_the_first_period(phrase):
    """
    A comparison names two windows. The router derives the earlier one itself,
    so resolving the first named period is correct -- and far better than
    returning None and asking the user again.
    """
    r = resolve_date_phrase(phrase, "UTC")
    assert r is not None, f"{phrase!r} did not resolve"
    assert r.label == "this_month"


def test_forward_windows_resolve_for_bill_questions():
    for phrase, label in (("next week", "next_week"), ("next month", "next_month")):
        r = resolve_date_phrase(phrase, "UTC")
        assert r is not None and r.label == label


def test_genuinely_unknown_phrases_still_return_none():
    """Canonicalisation must not start guessing: unknown stays unknown."""
    for phrase in ("banana", "", "   ", "sometime soon", "when I was younger"):
        assert resolve_date_phrase(phrase, "UTC") is None


# --- partial-month comparisons ----------------------------------------------
#
# "This month vs last month" on the 22nd used to compare Aug 1-22 against
# Jul 10-31 (equal LENGTH). That is a fair number attached to a window nobody
# means, and the assistant then described it as "all of July". Comparing the
# same calendar days is what people intend.

def test_partial_month_compares_against_the_same_calendar_days():
    r = previous_calendar_period(
        ResolvedDateRange(start=date(2026, 8, 1), end=date(2026, 8, 22), label="this_month")
    )
    assert (r.start, r.end) == (date(2026, 7, 1), date(2026, 7, 22))


def test_full_month_compares_against_the_whole_previous_month():
    r = previous_calendar_period(
        ResolvedDateRange(start=date(2026, 7, 1), end=date(2026, 7, 31), label="last_month")
    )
    assert (r.start, r.end) == (date(2026, 6, 1), date(2026, 6, 30))


def test_day_of_month_is_clamped_to_the_shorter_previous_month():
    """31 March has no counterpart in February; clamp rather than crash."""
    r = previous_calendar_period(
        ResolvedDateRange(start=date(2026, 3, 1), end=date(2026, 3, 31), label="this_month")
    )
    assert (r.start, r.end) == (date(2026, 2, 1), date(2026, 2, 28))


def test_january_rolls_back_into_the_previous_year():
    r = previous_calendar_period(
        ResolvedDateRange(start=date(2026, 1, 1), end=date(2026, 1, 15), label="this_month")
    )
    assert (r.start, r.end) == (date(2025, 12, 1), date(2025, 12, 15))


def test_non_month_ranges_fall_back_to_equal_length():
    """Calendar days are meaningless for "past 30 days"; keep equal length."""
    original = ResolvedDateRange(start=date(2026, 7, 10), end=date(2026, 8, 8), label="past_30_days")
    r = previous_calendar_period(original)
    assert (r.end - r.start) == (original.end - original.start)
    assert r.end < original.start


def test_plural_period_phrases_resolve():
    """A comparison naturally pluralises: "the current calendar months"."""
    for phrase in ("current calendar months", "current months", "previous months"):
        assert resolve_date_phrase(phrase, "UTC") is not None, phrase


def test_a_comparison_phrase_resolves_to_the_later_window():
    """
    The router treats the resolved range as the CURRENT period and derives the
    baseline from it. Both writing orders mean the same comparison, so both
    must yield the later window -- taking the first half of "last month vs
    this month" reported July against June for a question about July against
    August, and nothing in the output looked wrong.
    """
    later = resolve_date_phrase("this month", "America/New_York")
    assert later is not None

    for phrase in (
        "this month vs last month",
        "last month vs this month",
        "last month vs. this month",
        "this month compared to last month",
        "last month compared with this month",
        "this month versus last month",
    ):
        got = resolve_date_phrase(phrase, "America/New_York")
        assert got is not None, phrase
        assert (got.start, got.end) == (later.start, later.end), phrase


def test_a_shared_noun_comparison_resolves_too():
    """"the current and previous calendar months" has no separator to split on."""
    for phrase, expected in (
        ("the current and previous calendar months", "this month"),
        ("this and last month", "this month"),
        ("current and previous months", "this month"),
        ("this and last week", "this week"),
        ("the current and previous quarters", "this quarter"),
        ("this and last year", "this year"),
    ):
        got = resolve_date_phrase(phrase, "America/New_York")
        want = resolve_date_phrase(expected, "America/New_York")
        assert got is not None, phrase
        assert (got.start, got.end) == (want.start, want.end), phrase


def test_a_single_period_phrase_is_untouched_by_comparison_handling():
    for phrase, label in (
        ("this month", "this_month"),
        ("last month", "last_month"),
        ("last 30 days", "past_30_days"),
        ("this year", "this_year"),
        ("last week", "last_week"),
    ):
        got = resolve_date_phrase(phrase, "America/New_York")
        assert got is not None and got.label == label, phrase


def test_a_named_weekday_resolves_to_a_single_past_day():
    """
    "how much did I spend last friday?" is an ordinary question. Without
    weekday support it resolved to nothing, which reached the user as a
    question about which Friday they meant -- and no answer to it could
    resolve either, so the second answer was met with a third question.
    """
    from datetime import timedelta

    tz = "America/New_York"
    today = current_date_in(tz)

    for phrase in ("last friday", "friday", "most recent friday", "this past friday", "on friday"):
        got = resolve_date_phrase(phrase, tz)
        assert got is not None, phrase
        assert got.start == got.end, "a weekday names one day, not a range"
        assert got.start.weekday() == 4, phrase
        assert got.start <= today, "a spending question looks backward"
        assert today - got.start < timedelta(days=8), phrase


def test_last_weekday_never_means_today():
    """On a Friday, "last friday" is a week ago -- but a bare "friday" is today."""
    from datetime import timedelta

    tz = "America/New_York"
    today = current_date_in(tz)
    today_name = today.strftime("%A").lower()

    bare = resolve_date_phrase(today_name, tz)
    assert bare is not None and bare.start == today

    qualified = resolve_date_phrase(f"last {today_name}", tz)
    assert qualified is not None
    assert qualified.start == today - timedelta(days=7)


def test_every_weekday_name_and_abbreviation_resolves():
    tz = "America/New_York"
    names = {
        "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
        "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
    }
    for name, weekday in names.items():
        got = resolve_date_phrase(f"last {name}", tz)
        assert got is not None, name
        assert got.start.weekday() == weekday, name


def test_a_weekday_inside_a_longer_phrase_is_not_mistaken_for_a_day():
    """Only a phrase that IS a weekday resolves to one."""
    for phrase in ("friday to sunday", "every friday", "the friday after next"):
        got = resolve_date_phrase(phrase, "America/New_York")
        if got is not None:
            assert got.start != got.end or "friday" not in phrase.split()[0], phrase


def test_a_specific_calendar_date_resolves_to_that_one_day():
    """
    Before this, "how much did I spend on August 20?" resolved to nothing and
    the planner substituted "this week" -- answering about August 17-23. The
    window was stated, so it was visible rather than silently wrong, but it
    was not the question that was asked.
    """
    tz = "America/New_York"
    for phrase in ("August 20", "Aug 20", "20 August", "20th August", "2026-08-20", "on August 20"):
        got = resolve_date_phrase(phrase, tz)
        assert got is not None, phrase
        assert got.start == got.end, phrase
        assert (got.start.month, got.start.day) == (8, 20), phrase


def test_a_year_is_inferred_backwards():
    """A spending assistant is asked about money already spent."""
    tz = "America/New_York"
    today = current_date_in(tz)
    for phrase in ("January 5", "December 20", "March 3"):
        got = resolve_date_phrase(phrase, tz)
        assert got is not None, phrase
        assert got.start <= today, f"{phrase} resolved into the future"
        assert (today - got.start).days < 366, phrase


def test_an_explicit_year_is_honoured():
    got = resolve_date_phrase("August 20, 2024", "America/New_York")
    assert got is not None and got.start == date(2024, 8, 20)


def test_an_explicit_range_resolves_inclusively():
    tz = "America/New_York"
    expected = (date(2026, 8, 1), date(2026, 8, 15))
    for phrase in (
        "August 1 to August 15",
        "between August 1 and August 15",
        "from August 1 to August 15",
        "August 1 to 15",
        "1 - 15 August",
    ):
        got = resolve_date_phrase(phrase, tz)
        assert got is not None, phrase
        assert (got.start, got.end) == expected, phrase


def test_a_bare_day_of_month_steps_back_when_it_has_not_happened():
    """On the 23rd, "the 31st" is last month's 31st, not one eight days away."""
    tz = "America/New_York"
    today = current_date_in(tz)
    got = resolve_date_phrase("the 31st", tz)
    assert got is not None
    assert got.start.day == 31
    assert got.start <= today


def test_an_impossible_date_resolves_to_nothing_rather_than_raising():
    for phrase in ("February 30", "April 31", "2026-13-01", "2026-02-30"):
        assert resolve_date_phrase(phrase, "America/New_York") is None, phrase


def test_relative_phrases_are_unaffected_by_absolute_parsing():
    for phrase, label in (
        ("this month", "this_month"),
        ("last month", "last_month"),
        ("this week", "this_week"),
        ("last 30 days", "past_30_days"),
    ):
        got = resolve_date_phrase(phrase, "America/New_York")
        assert got is not None and got.label == label, phrase


def test_a_recurring_weekday_inside_a_period():
    """
    "Fridays in June" is a window plus a weekday, not a date range -- the days
    are scattered through the month. It resolved to nothing before, and the
    assistant asked which Fridays were meant three separate times, unable to
    accept any answer.
    """
    from app.utils.dates import resolve_weekday_in_period

    tz = "America/New_York"
    for phrase in ("fridays in june", "every friday in june", "all fridays in june"):
        got = resolve_weekday_in_period(phrase, tz)
        assert got is not None, phrase
        window, weekday = got
        assert weekday == 4, phrase
        assert (window.start.month, window.start.day) == (6, 1), phrase

    monday = resolve_weekday_in_period("every monday last month", tz)
    assert monday is not None and monday[1] == 0


def test_phrases_that_are_not_a_recurring_weekday():
    """It must not claim shapes it does not mean."""
    from app.utils.dates import resolve_weekday_in_period

    tz = "America/New_York"
    # A single named day, handled by the one-day path.
    assert resolve_weekday_in_period("last friday", tz) is None
    # A month, with no weekday in it.
    assert resolve_weekday_in_period("june 2026", tz) is None
    # A weekday with no window: guessing one would answer a different question.
    assert resolve_weekday_in_period("every friday", tz) is None


# --------------------------------------------------- multi-period phrases
#
# "How much did I spend in the last 2 months?" resolved to nothing, and an
# unresolvable phrase becomes a clarifying question -- so the assistant asked
# whether the user meant "two full calendar months or the last 60 days", about
# a phrase with one obvious meaning. The cause was the pluralisation rule for
# comparison phrases ("the current calendar months" -> "month") firing on a
# COUNTED phrase and turning "last 2 months" into "last 2 month", which
# matched nothing at all.


@pytest.mark.parametrize(
    "phrase",
    ["last 2 months", "past 2 months", "last two months", "previous 2 months", "the last 2 months"],
)
def test_a_counted_month_phrase_resolves_rather_than_asking(phrase):
    resolved = resolve_date_phrase(phrase, "UTC")
    assert resolved is not None, f"{phrase!r} would have become a clarifying question"
    assert resolved.label == "past_2_months"


def test_counted_periods_are_trailing_windows_not_whole_calendar_months():
    """
    Starting at the first of the month two back made "the last 2 months" span
    June 1 to August 29 -- three months of spending presented as two, with
    every total inflated and nothing in the answer to show it.
    """
    today = current_date_in("UTC")
    resolved = resolve_date_phrase("last 2 months", "UTC")
    assert resolved.end == today
    assert 58 <= (resolved.end - resolved.start).days <= 63, "about two months, not three"


@pytest.mark.parametrize(
    "phrase,label",
    [
        ("last 3 months", "past_3_months"),
        ("past 6 months", "past_6_months"),
        ("last 2 weeks", "past_2_weeks"),
        ("past 1 month", "past_1_months"),
        ("last 12 months", "past_12_months"),
        ("past year", "past_12_months"),
    ],
)
def test_other_counted_windows_resolve(phrase, label):
    resolved = resolve_date_phrase(phrase, "UTC")
    assert resolved is not None and resolved.label == label


def test_the_past_year_is_not_the_last_calendar_year():
    """Conflating them shifts every figure by up to a full year."""
    trailing = resolve_date_phrase("past year", "UTC")
    calendar = resolve_date_phrase("last year", "UTC")
    assert trailing.end == current_date_in("UTC")
    assert calendar.end.year == current_date_in("UTC").year - 1
    assert trailing.start != calendar.start


def test_an_uncounted_plural_still_reduces_to_its_singular():
    """The comparison-phrase rule this sits beside must keep working."""
    assert resolve_date_phrase("current calendar months", "UTC").label == "this_month"
    assert resolve_date_phrase("previous months", "UTC").label == "last_month"


def test_a_month_written_once_governs_both_halves_of_a_range(monkeypatch):
    """
    A bare day number never FAILS to resolve -- it silently resolves against
    today's month. So "1 - 15 August" produced start=1 September, end=15
    August, an end before its start, which is discarded as unresolvable; the
    user was asked to clarify a range they had stated exactly.

    The bug was invisible for most of any given month: asked during August the
    same phrase resolved perfectly. This pins the clock so it cannot pass by
    luck of the calendar again.
    """
    import app.utils.dates as dates

    for today in (date(2026, 8, 5), date(2026, 9, 3), date(2026, 12, 31)):
        monkeypatch.setattr(dates, "_now_in_tz", lambda _tz, d=today: datetime(d.year, d.month, d.day))
        for phrase in ("1 - 15 August", "August 1 to 15", "August 1 to August 15", "1 to 15 August"):
            got = resolve_date_phrase(phrase, "UTC")
            assert got is not None, f"{phrase!r} with today={today}"
            assert (got.start.month, got.start.day) == (8, 1), f"{phrase!r} with today={today}"
            assert (got.end.month, got.end.day) == (8, 15), f"{phrase!r} with today={today}"
            assert got.start <= got.end
