"""
Deterministic natural-language date range resolution.

This is intentionally NOT delegated to the LLM. A phrase like "last month"
must resolve identically every time for the same user/timezone/now, so
calculations stay reproducible and testable. The planner LLM only decides
*that* a date phrase is present in the user's message (or reuses one from
conversation context); resolving it to concrete start/end dates always goes
through this module.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.schemas.planner import ResolvedDateRange


def _now_in_tz(timezone: str) -> datetime:
    try:
        tz = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - unknown/invalid tz string from upstream data
        tz = ZoneInfo("UTC")
    return datetime.now(tz)


def current_date_in(timezone: str) -> date:
    """Today, in the user's own timezone. The public form of _now_in_tz."""
    return _now_in_tz(timezone).date()


def _quarter_bounds(d: date) -> tuple[date, date]:
    quarter_start_month = ((d.month - 1) // 3) * 3 + 1
    start = date(d.year, quarter_start_month, 1)
    if quarter_start_month == 10:
        end = date(d.year, 12, 31)
    else:
        next_q_start = date(d.year, quarter_start_month + 3, 1)
        end = next_q_start - timedelta(days=1)
    return start, end


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last day of a calendar month. Public: callers outside this
    module need month boundaries too, and each one re-deriving them is how
    two parts of a financial service come to disagree about when a month
    ends."""
    return _month_bounds(year, month)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


# Comparison connectives. "this month vs last month" describes a comparison,
# not a single window. The router resolves the CURRENT period and derives the
# earlier one itself, so a compound phrase must yield the LATER of the two
# windows named -- not simply the first. Users and models write the pair in
# both orders ("last month vs this month" is just as natural), and taking the
# first half of that inverts the comparison: it reported July against June for
# a question about July against August, with no sign anything was wrong.
_COMPARISON_SPLITS = (
    " compared to ",
    " compared with ",
    " versus ",
    " vs. ",
    " vs ",
    " against ",
    " and the ",
    " to the ",
)

# Filler that changes nothing about which window is meant.
_NOISE = (
    "the ",
    "for ",
    "in ",
    "during ",
    "over ",
    "of ",
    " period",
    " calendar",
    " so far",
    " to date",
    " entire",
    " whole",
    " full",
)

# Synonyms the planner reaches for that mean an existing canonical phrase.
_SYNONYMS = {
    "current month": "this month",
    "present month": "this month",
    "month to date": "this month",
    "mtd": "this month",
    "previous month": "last month",
    "prior month": "last month",
    "preceding month": "last month",
    "current week": "this week",
    "previous week": "last week",
    "prior week": "last week",
    "past weekend": "last weekend",
    "previous weekend": "last weekend",
    "prior weekend": "last weekend",
    "current year": "this year",
    "year to date": "this year",
    "ytd": "this year",
    "previous year": "last year",
    "prior year": "last year",
    "current quarter": "this quarter",
    "previous quarter": "last quarter",
    "prior quarter": "last quarter",
    "quarter to date": "this quarter",
    "today so far": "today",
    # A trailing twelve months, which is what "the past year" means. Distinct
    # from "last year", which is the previous CALENDAR year and is resolved
    # separately -- conflating them shifts every figure by up to a year.
    "past year": "past 12 months",
    "past 12 month": "past 12 months",
    "trailing year": "past 12 months",
    "last 12 month": "past 12 months",
}

# Counts people write as words. Every rule that reads a period length keys on
# a digit, so these are normalised before any of them run.
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "couple": "2", "few": "3",
}

# A comparison can also be written with the noun shared between both halves:
# "the current and previous calendar months", "this and last month". There is
# no separator to split on -- the two windows are distributed across one noun
# phrase. The router derives the earlier period itself, so these reduce to the
# LATER window, exactly like the "A vs B" forms.
_DISTRIBUTED_COMPARISON = re.compile(
    r"\b(?:this|current|the current)\s+and\s+(?:last|previous|prior|preceding|the previous)\s+"
    r"(?:calendar\s+)?(month|week|quarter|year)s?\b"
)


_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# "last friday", "this past Tuesday", "on wednesday". A named weekday is one of
# the most ordinary ways to ask about a day's spending, and without this the
# phrase resolved to nothing -- which surfaced to the user as a question about
# which Friday they meant, then another one, because no answer to it could
# ever resolve either.
_WEEKDAY_PHRASE = re.compile(
    r"^(?:on\s+)?(?P<qualifier>last|this past|past|this|the most recent|most recent|previous)?\s*"
    r"(?P<day>" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")$"
)


def _resolve_weekday(normalized: str, today: date) -> ResolvedDateRange | None:
    """
    A single past day named by its weekday.

    Everything here looks BACKWARD. This is a spending assistant: "friday"
    means the Friday that has been, not one that has not happened yet and
    therefore has no transactions.

    "last friday" means strictly before today, so on a Friday it is the Friday
    a week ago -- while a bare "friday" that same day means today. That
    distinction is the whole reason the qualifier is captured.
    """
    m = _WEEKDAY_PHRASE.match(normalized)
    if m is None:
        return None

    target = _WEEKDAYS[m.group("day")]
    delta = (today.weekday() - target) % 7

    qualifier = (m.group("qualifier") or "").strip()
    if delta == 0 and qualifier in ("last", "previous"):
        # Today IS that weekday, but "last friday" cannot mean today.
        delta = 7

    day = today - timedelta(days=delta)
    return ResolvedDateRange(start=day, end=day, label=f"{m.group('day')}_{day.isoformat()}")


def _canonicalize(phrase: str) -> str:
    """
    Reduce a free-text period phrase to one of the canonical forms below.

    The planner is an LLM: it says "current calendar month" or "last 30 days"
    just as readily as "this month". Exact string matching turned every such
    variant into an unresolvable phrase, which surfaced to the user as a
    clarifying question instead of an answer.
    """
    text = " ".join(phrase.strip().lower().split())
    for ch in ",.!?\"'":
        text = text.replace(ch, " ")
    text = " ".join(text.split())

    text = _DISTRIBUTED_COMPARISON.sub(lambda m: f"this {m.group(1)}", text)

    for sep in _COMPARISON_SPLITS:
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break

    if text in _SYNONYMS:
        return _SYNONYMS[text]

    for noise in _NOISE:
        if noise.startswith(" "):
            text = text.replace(noise, " ")
        elif text.startswith(noise):
            text = text[len(noise):]
        text = " ".join(text.split())

    # "last two months" -> "last 2 months". People write small counts as
    # words at least as often as digits, and every rule below keys on a digit.
    parts = text.split()
    text = " ".join(_NUMBER_WORDS.get(part, part) for part in parts)

    # The planner often pluralises when a comparison names two windows
    # ("the current and previous calendar months").
    #
    # A COUNTED phrase is exempt: "last 2 months" is plural because there are
    # two of them, and singularising it produced "last 2 month", which matched
    # no rule at all. The phrase then reached the user as "did you mean the
    # last two calendar months or the last 60 days?" -- a clarifying question
    # about a phrase with one obvious meaning, and the exact failure the
    # planner prompt forbids. The guard checked the FIRST token for a digit,
    # but in "last 2 months" the digit is the second.
    counted = any(part.isdigit() for part in text.split())
    if not counted:
        for singular, plural in (
            ("month", "months"), ("week", "weeks"), ("quarter", "quarters"), ("year", "years"),
        ):
            if text.endswith(" " + plural):
                text = text[: -len(plural)] + singular
                break

    if text in _SYNONYMS:
        return _SYNONYMS[text]

    # "last 30 days" means the same window as "past 30 days".
    parts = text.split()
    if len(parts) == 3 and parts[0] in ("last", "past", "previous", "prior") and parts[1].isdigit():
        # "past 1 month" counts as much as "past 2 months"; the rules below
        # read the plural form only.
        unit = parts[2] if parts[2].endswith("s") else parts[2] + "s"
        if unit in ("days", "weeks", "months", "years"):
            text = f"past {parts[1]} {unit}"

    return _SYNONYMS.get(text, text)


def _split_comparison(phrase: str) -> tuple[str, str] | None:
    """The two halves of "A vs B", or None if the phrase names a single window."""
    text = " ".join(phrase.strip().lower().split())
    for sep in _COMPARISON_SPLITS:
        if sep in text:
            left, right = text.split(sep, 1)
            if left.strip() and right.strip():
                return left.strip(), right.strip()
    return None


def _later_half_of_comparison(phrase: str, timezone: str) -> ResolvedDateRange | None:
    """
    For a compound phrase, the window the caller wants is the LATER one.

    "last month vs this month" and "this month vs last month" mean the same
    comparison; only the writing order differs. The router derives the earlier
    period from the later one, so returning whichever half came first would
    silently shift the whole comparison back by a period.
    """
    halves = _split_comparison(phrase)
    if halves is None:
        return None

    resolved = [r for r in (resolve_date_phrase(h, timezone) for h in halves) if r is not None]
    if not resolved:
        return None
    return max(resolved, key=lambda r: (r.end, r.start))


_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORDINAL = r"(?:st|nd|rd|th)?"

# "August 20", "Aug 20th, 2026"
_MONTH_FIRST = re.compile(
    rf"^(?P<month>{_MONTH_NAMES})\s+(?P<day>\d{{1,2}}){_ORDINAL}(?:,?\s+(?P<year>\d{{4}}))?$"
)
# "20 August", "20th Aug 2026"
_DAY_FIRST = re.compile(
    rf"^(?P<day>\d{{1,2}}){_ORDINAL}\s+(?P<month>{_MONTH_NAMES})(?:,?\s+(?P<year>\d{{4}}))?$"
)
# "2026-08-20"
_ISO = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})$")
# "the 20th", "on the 3rd" -- a day in the current or most recent month.
_BARE_DAY = re.compile(rf"^(?:the\s+)?(?P<day>\d{{1,2}}){_ORDINAL}$")

# "August 1 to August 15", "between Aug 1 and Aug 15", "1 - 15 August".
_RANGE_SEPARATORS = (" to ", " through ", " thru ", " until ", " till ", " up to ", " and ", " - ", " -- ")


def _month_name_of(text: str) -> str | None:
    for token in text.split():
        if token in _MONTHS:
            return token
    return None


def _resolve_absolute_day(text: str, today: date) -> date | None:
    """
    One specific calendar day, or None.

    A year is inferred when the user does not give one, and inference looks
    BACKWARD: this assistant answers questions about money already spent, so
    "December 20" asked in August means last December, not one four months
    away that has no transactions in it.
    """
    m = _ISO.match(text)
    if m:
        try:
            return date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
        except ValueError:
            return None

    m = _MONTH_FIRST.match(text) or _DAY_FIRST.match(text)
    if m:
        month = _MONTHS[m.group("month")]
        day = int(m.group("day"))
        year_text = m.group("year")
        if year_text:
            try:
                return date(int(year_text), month, day)
            except ValueError:
                return None
        for year in (today.year, today.year - 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate <= today:
                return candidate
        return None

    m = _BARE_DAY.match(text)
    if m:
        day = int(m.group("day"))
        year, month = today.year, today.month
        for _ in range(2):
            # The 31st does not exist in every month; step back and try again.
            try:
                candidate = date(year, month, day)
            except ValueError:
                pass
            else:
                if candidate <= today:
                    return candidate
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        return None

    return None


def _resolve_absolute_range(text: str, today: date) -> ResolvedDateRange | None:
    """
    An explicit span between two calendar dates, inclusive.

    Handles the shorthand where the month is written once -- "August 1 to 15"
    and "1 - 15 August" both mean the same fortnight, and a user who has just
    typed the month does not expect to type it again.
    """
    text = text.removeprefix("between ").removeprefix("from ").strip()

    for sep in _RANGE_SEPARATORS:
        if sep not in text:
            continue
        left, right = (part.strip() for part in text.split(sep, 1))
        if not left or not right:
            continue

        left_month, right_month = _month_name_of(left), _month_name_of(right)

        start = _resolve_absolute_day(left, today)
        end = _resolve_absolute_day(right, today)

        # A month written once governs BOTH halves. This has to run whenever
        # one side names a month and the other does not -- not only when the
        # bare side failed to resolve, because a bare day number never fails:
        # it silently resolves against TODAY's month.
        #
        # That made the bug time-dependent, which is the worst kind to ship.
        # "1 - 15 August" asked in August gave 1 Aug - 15 Aug and looked
        # perfect; asked in September it gave start=1 Sep, end=15 Aug, and an
        # end before its start is discarded as unresolvable -- so the user got
        # a clarifying question about a date range they had stated exactly.
        # The test for it passed every day of August and failed on the 1st.
        if right_month and not left_month:
            start = _resolve_absolute_day(f"{right_month} {left}", today) or start
        elif left_month and not right_month:
            end = _resolve_absolute_day(f"{left_month} {right}", today) or end

        if start is not None and end is not None and end < start and end.year < start.year:
            # A range reads forward, and a lone date resolves BACKWARD -- to
            # the most recent occurrence that has actually happened. Asked on
            # 5 August, the "15 August" in "1 - 15 August" is still in the
            # future, so it resolves to LAST August and the range spans a
            # negative fortnight across a year boundary.
            #
            # Correct only a year rollover, never a genuinely backwards range:
            # "August 15 to August 1" has both halves in the same year and is
            # left unresolved, because the user really did write it backwards
            # and guessing which end they meant would answer a question nobody
            # asked.
            try:
                pulled_forward = end.replace(year=end.year + 1)
            except ValueError:  # 29 February in a non-leap year
                pulled_forward = None
            if pulled_forward is not None and pulled_forward >= start:
                end = pulled_forward

        if start is None or end is None or end < start:
            continue
        return ResolvedDateRange(start=start, end=end, label=f"{start.isoformat()}_to_{end.isoformat()}")

    return None


def _resolve_named_month(text: str, today: date) -> ResolvedDateRange | None:
    """
    A whole month named outright: "July", "July 2026", "last July".

    With two years of history behind it, naming the month is one of the most
    natural ways to ask a question, and it is not the same shape as
    "July 20" -- there is no day in it. Without this the phrase resolved to
    nothing and the user was asked which dates they meant, about a month they
    had just named.
    """
    words = text.replace(",", " ").split()
    if not words:
        return None

    year: int | None = None
    if words[-1].isdigit() and len(words[-1]) == 4:
        year = int(words[-1])
        words = words[:-1]

    words = [w for w in words if w not in ("in", "of", "during", "the", "month", "last", "this", "past")]
    if len(words) != 1 or words[0] not in _MONTHS:
        return None

    month = _MONTHS[words[0]]
    if year is None:
        # The most recent occurrence that has actually happened. Asked in
        # August, "July" is this year's; "December" is last year's.
        year = today.year if month <= today.month else today.year - 1

    start, end = _month_bounds(year, month)
    return ResolvedDateRange(start=start, end=min(end, today), label=f"{year}-{month:02d}")


def _resolve_named_year(text: str, today: date) -> ResolvedDateRange | None:
    """A bare calendar year: "2025", "in 2025"."""
    words = [w for w in text.replace(",", " ").split() if w not in ("in", "of", "during", "the", "year")]
    if len(words) != 1 or not words[0].isdigit() or len(words[0]) != 4:
        return None
    year = int(words[0])
    if not (1900 <= year <= today.year):
        return None
    return ResolvedDateRange(
        start=date(year, 1, 1), end=min(date(year, 12, 31), today), label=str(year)
    )


def _resolve_absolute(normalized: str, today: date) -> ResolvedDateRange | None:
    # "on August 20", "on the 20th" -- the preposition carries no meaning here
    # and is not stripped upstream, where "on" is too short to remove safely.
    normalized = normalized.removeprefix("on ").strip()

    span = _resolve_absolute_range(normalized, today)
    if span is not None:
        return span

    month = _resolve_named_month(normalized, today)
    if month is not None:
        return month

    year = _resolve_named_year(normalized, today)
    if year is not None:
        return year

    day = _resolve_absolute_day(normalized, today)
    if day is not None:
        return ResolvedDateRange(start=day, end=day, label=day.isoformat())

    return None


_EVERY_WEEKDAY = re.compile(
    rf"^(?:every|all|each)?\s*(?P<day>{'|'.join(sorted(_WEEKDAYS, key=len, reverse=True))})s?"
    r"(?:\s+(?:in|of|during))?\s*(?P<rest>.*)$"
)


def resolve_weekday_in_period(phrase: str, timezone: str) -> tuple[ResolvedDateRange, int] | None:
    """
    "Fridays in June", "every Monday last month" -> (the window, the weekday).

    A recurring weekday inside a period is not a date range: the days are
    scattered through it. So the caller gets the surrounding window plus the
    weekday to filter by, and the filtering happens where the transactions
    are. Returns None for anything that is not this shape, including a single
    named day like "last friday", which _resolve_weekday already handles.
    """
    text = _canonicalize(phrase)
    match = _EVERY_WEEKDAY.match(text)
    if match is None:
        return None

    rest = match.group("rest").strip()
    if not rest:
        # "every friday" with no period named -- the window is unstated, and
        # guessing one silently would answer a question nobody asked.
        return None

    window = resolve_date_phrase(rest, timezone)
    if window is None:
        return None
    return window, _WEEKDAYS[match.group("day")]


# "the rest of the month", "remainder of this month", "until the end of the
# month". Forward-looking, so it runs from today to month end -- the one window
# the resolver otherwise could not express, which turned "how much do I owe in
# bills for the rest of this month?" into a clarifying question.
_REST_OF_MONTH = re.compile(
    r"\b(?:rest|remainder|remaining(?:\s+days?)?)\s+of\s+(?:the\s+|this\s+)?(?:current\s+)?month\b"
    r"|\b(?:until|till|through|to|by)\s+(?:the\s+)?end\s+of\s+(?:the\s+|this\s+)?month\b"
    r"|\bend\s+of\s+(?:the\s+|this\s+)month\b"
    r"|\bmonth[- ]end\b"
)


def resolve_date_phrase(phrase: str, timezone: str) -> ResolvedDateRange | None:
    """
    Resolve a natural-language date phrase to a concrete inclusive date range
    in the user's timezone. Returns None if the phrase is not recognized —
    callers should treat that as "no date range specified", not an error.
    """
    later = _later_half_of_comparison(phrase, timezone)
    if later is not None:
        return later

    normalized = _canonicalize(phrase)
    today = _now_in_tz(timezone).date()

    if _REST_OF_MONTH.search((phrase or "").lower()):
        _, month_end = _month_bounds(today.year, today.month)
        return ResolvedDateRange(start=today, end=month_end, label="rest_of_month")

    weekday = _resolve_weekday(normalized, today)
    if weekday is not None:
        return weekday

    absolute = _resolve_absolute(normalized, today)
    if absolute is not None:
        return absolute

    if normalized == "today":
        return ResolvedDateRange(start=today, end=today, label="today")

    if normalized == "yesterday":
        d = today - timedelta(days=1)
        return ResolvedDateRange(start=d, end=d, label="yesterday")

    if normalized == "this week":
        start = today - timedelta(days=today.weekday())
        return ResolvedDateRange(start=start, end=today, label="this_week")

    if normalized == "last week":
        this_week_start = today - timedelta(days=today.weekday())
        start = this_week_start - timedelta(days=7)
        end = this_week_start - timedelta(days=1)
        return ResolvedDateRange(start=start, end=end, label="last_week")

    if normalized == "this month":
        start, _ = _month_bounds(today.year, today.month)
        return ResolvedDateRange(start=start, end=today, label="this_month")

    if normalized == "last month":
        first_of_this_month = date(today.year, today.month, 1)
        last_month_end = first_of_this_month - timedelta(days=1)
        start, end = _month_bounds(last_month_end.year, last_month_end.month)
        return ResolvedDateRange(start=start, end=end, label="last_month")

    if normalized == "this quarter":
        start, _ = _quarter_bounds(today)
        return ResolvedDateRange(start=start, end=today, label="this_quarter")

    if normalized == "last quarter":
        this_q_start, _ = _quarter_bounds(today)
        last_q_end = this_q_start - timedelta(days=1)
        start, end = _quarter_bounds(last_q_end)
        return ResolvedDateRange(start=start, end=end, label="last_quarter")

    if normalized == "this year":
        return ResolvedDateRange(start=date(today.year, 1, 1), end=today, label="this_year")

    if normalized == "last year":
        return ResolvedDateRange(
            start=date(today.year - 1, 1, 1), end=date(today.year - 1, 12, 31), label="last_year"
        )

    if normalized == "next week":
        this_week_start = today - timedelta(days=today.weekday())
        start = this_week_start + timedelta(days=7)
        return ResolvedDateRange(start=start, end=start + timedelta(days=6), label="next_week")

    if normalized == "next month":
        first_of_this = date(today.year, today.month, 1)
        nxt = date(first_of_this.year + 1, 1, 1) if first_of_this.month == 12 else date(
            first_of_this.year, first_of_this.month + 1, 1
        )
        start, end = _month_bounds(nxt.year, nxt.month)
        return ResolvedDateRange(start=start, end=end, label="next_month")

    if normalized == "this weekend":
        # Saturday/Sunday of the current week (Mon=0 .. Sun=6)
        saturday = today - timedelta(days=today.weekday()) + timedelta(days=5)
        sunday = saturday + timedelta(days=1)
        return ResolvedDateRange(start=saturday, end=sunday, label="this_weekend")

    if normalized == "last weekend":
        # The Saturday/Sunday of the week before this one -- NOT the same as
        # shifting "this weekend" back by its own length (that lands on
        # Thursday/Friday; see previous_calendar_period below for the
        # matching bug on the comparison side).
        this_saturday = today - timedelta(days=today.weekday()) + timedelta(days=5)
        saturday = this_saturday - timedelta(days=7)
        sunday = saturday + timedelta(days=1)
        return ResolvedDateRange(start=saturday, end=sunday, label="last_weekend")

    for prefix, direction in (("past ", -1), ("next ", 1)):
        if normalized.startswith(prefix) and normalized.endswith(" days"):
            middle = normalized[len(prefix) : -len(" days")].strip()
            if middle.isdigit():
                n = int(middle)
                if direction < 0:
                    return ResolvedDateRange(
                        start=today - timedelta(days=n - 1), end=today, label=f"past_{n}_days"
                    )
                return ResolvedDateRange(
                    start=today, end=today + timedelta(days=n), label=f"next_{n}_days"
                )

    if normalized.startswith("past ") and normalized.endswith(" weeks"):
        middle = normalized[len("past ") : -len(" weeks")].strip()
        if middle.isdigit():
            n = int(middle)
            return ResolvedDateRange(
                start=today - timedelta(weeks=n) + timedelta(days=1), end=today, label=f"past_{n}_weeks"
            )

    for unit, months_each in (("months", 1), ("years", 12)):
        if normalized.startswith("past ") and normalized.endswith(f" {unit}"):
            middle = normalized[len("past ") : -len(f" {unit}")].strip()
            if middle.isdigit():
                n = int(middle) * months_each
                # A TRAILING window, not a run of whole calendar months.
                # Starting at the first of the month N back made "the last 2
                # months" cover June 1 to August 29 -- three months of
                # spending reported as two, and every total inflated by half.
                # Trailing also matches "last 60 days", which people use
                # interchangeably with "last 2 months".
                return ResolvedDateRange(
                    start=_months_before(today, n) + timedelta(days=1), end=today,
                    label=f"past_{n}_months",
                )

    return None


def _months_before(anchor: date, months: int) -> date:
    """
    The same day-of-month, `months` earlier, clamped to a real date.

    31 March minus one month has no answer in the calendar; the last day of
    February is the only one that does not skip past the month entirely.
    """
    year = anchor.year
    month = anchor.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, min(anchor.day, _days_in_month(year, month)))


def _days_in_month(year: int, month: int) -> int:
    _, end = _month_bounds(year, month)
    return end.day


def previous_calendar_period(date_range: ResolvedDateRange) -> ResolvedDateRange:
    """
    The equivalent window in the previous month: SAME CALENDAR DAYS.

    For a partial month this is what people mean by "this month vs last
    month" -- August 1-22 compares against July 1-22, not against all of
    July. Comparing 22 days against 31 makes the current month look lower
    every time, which is a misleading answer rather than a wrong number.

    Falls back to an equal-length window for ranges that are not
    month-scoped (e.g. "past 30 days"), where calendar days mean nothing.
    """
    start, end = date_range.start, date_range.end

    # A weekend is always Saturday-Sunday. Shifting it back by its own
    # length (duration + 1 day, same as any other short range) lands on
    # Thursday-Friday, not the actual previous weekend -- confirmed live:
    # "did I spend more than usual this weekend?" compared against Thu/Fri.
    if "weekend" in date_range.label:
        return ResolvedDateRange(
            start=start - timedelta(days=7),
            end=end - timedelta(days=7),
            label="previous_" + date_range.label,
        )

    month_scoped = start.day == 1 and (start.year, start.month) == (end.year, end.month)
    if not month_scoped:
        return previous_period_of_same_length(date_range)

    prev_year, prev_month = (start.year - 1, 12) if start.month == 1 else (start.year, start.month - 1)
    # Clamp: 31 January has no counterpart in February.
    prev_end_day = min(end.day, _days_in_month(prev_year, prev_month))
    return ResolvedDateRange(
        start=date(prev_year, prev_month, 1),
        end=date(prev_year, prev_month, prev_end_day),
        label="previous_calendar_" + date_range.label,
    )


def previous_period_of_same_length(date_range: ResolvedDateRange) -> ResolvedDateRange:
    """
    Compute the period of identical length immediately preceding date_range.
    Used generically for period-over-period comparisons regardless of
    whether the original range was a day, week, month, or custom span.
    """
    duration = date_range.end - date_range.start
    previous_end = date_range.start - timedelta(days=1)
    previous_start = previous_end - duration
    return ResolvedDateRange(start=previous_start, end=previous_end, label="previous_" + date_range.label)


def month_before(date_range: ResolvedDateRange) -> ResolvedDateRange:
    """
    Given a resolved month-scale range, compute the equivalent range for the
    prior month. Used to resolve follow-ups like "what about the month before?"
    """
    first_of_period_month = date(date_range.start.year, date_range.start.month, 1)
    prev_month_end = first_of_period_month - timedelta(days=1)
    start, end = _month_bounds(prev_month_end.year, prev_month_end.month)
    return ResolvedDateRange(start=start, end=end, label="month_before_" + date_range.label)
