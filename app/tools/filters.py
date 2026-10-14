"""
One place where a plan's category and merchant filters meet real data.

WHY IT IS SHARED
    Filtering used to be a one-line comparison repeated in five modules --
    the expenses tool, the transactions tool, two calculation helpers and the
    backend client. Each was `(t.category or "").lower() == filter.lower()`,
    and each was wrong in the same way: the user's words are not the
    database's labels. Fixing that in one module and not the others would
    have produced something worse than the bug, which is two totals for the
    same question depending on which tool answered it.

WHAT IT DOES BEYOND MATCHING
    THE SLOTS GET SWAPPED. The planner decides whether a phrase is a category
    or a merchant, and it cannot always know: "how much on Gelato Bar?" is a
    merchant, "how much on groceries?" is a category, and a model looking at
    one sentence guesses. A guess in the wrong slot used to produce a
    confident $0.00. Here, a phrase that matches nothing in the slot it
    arrived in is retried in the other one before any total is computed.

    NOTHING MATCHING IS REPORTED, NOT ROUNDED TO ZERO. An empty result after
    filtering is ambiguous: the user may have spent nothing, or may have
    named something they do not have. Those need different answers, and only
    this layer knows which happened, so it says so in `notes` and hands over
    the nearest real merchant names instead of a bare zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.financial import Transaction
from app.utils.categories import (
    CategoryMatch,
    known_category_names,
    matches_category,
    matches_subcategory,
    resolve_category,
    subcategory_requested,
)
from app.utils.merchants import MerchantMatch, find_merchant_matches, suggest_merchants


@dataclass
class ResolvedFilters:
    """The transactions a plan's filters actually select, and how they got there."""

    transactions: list[Transaction]
    #: The category label filtered on, exactly as the data spells it.
    category: str | None = None
    #: The finer label filtered on, when the data supported one.
    subcategory: str | None = None
    #: What the user asked for, kept so the answer can name it.
    requested_category: str | None = None
    requested_merchant: str | None = None
    #: Display names of the merchants matched, grouped.
    merchants: list[str] = field(default_factory=list)
    merchant_matches: list[MerchantMatch] = field(default_factory=list)
    #: Close merchant names when the requested one matched nothing.
    suggestions: list[str] = field(default_factory=list)
    #: Plain-language disclosures the answer must carry: a loose match, a
    #: widened scope, a name that was not found.
    notes: list[str] = field(default_factory=list)
    #: True when a filter was applied and selected no transactions -- which is
    #: a different fact from "the total is zero".
    filtered_to_nothing: bool = False

    def as_dict(self) -> dict:
        """The subset a tool result should expose to the model."""
        out: dict = {}
        if self.requested_category:
            out["requested_category"] = self.requested_category
        if self.category:
            out["category"] = self.category
        if self.subcategory:
            out["subcategory"] = self.subcategory
        if self.requested_merchant:
            out["requested_merchant"] = self.requested_merchant
        if self.merchants:
            out["matched_merchants"] = self.merchants
        if self.suggestions:
            out["closest_merchants_in_data"] = self.suggestions
        if self.notes:
            out["filter_notes"] = self.notes
        return out


def _available_categories(transactions: list[Transaction]) -> set[str]:
    return {t.category for t in transactions if t.category}


def apply_filters(
    transactions: list[Transaction],
    *,
    category_filter: str | None = None,
    merchant_filter: str | None = None,
) -> ResolvedFilters:
    """
    Narrow `transactions` by a plan's category and merchant filters.

    Both filters are resolved against the data in hand, so the same call works
    whatever vocabulary the backend uses. Returns everything the answer needs
    to describe what it actually measured.
    """
    result = ResolvedFilters(transactions=list(transactions))
    result.requested_category = category_filter
    result.requested_merchant = merchant_filter

    category_term, merchant_term = category_filter, merchant_filter

    # A phrase in the wrong slot is retried in the other one. Done before any
    # filtering, so the totals below are computed once, on the right rows.
    if category_term and resolve_category(category_term, _available_categories(transactions)) is None:
        if find_merchant_matches(transactions, category_term):
            result.notes.append(
                f"'{category_term}' is not one of this user's spending categories, but it does "
                "match a merchant they have paid, so it was treated as a merchant name."
            )
            merchant_term, category_term = category_term, None

    if merchant_term and not find_merchant_matches(transactions, merchant_term):
        as_category = resolve_category(merchant_term, _available_categories(transactions))
        if as_category is not None:
            result.notes.append(
                f"'{merchant_term}' matches no merchant in this user's transactions, but it names "
                f"the spending category '{as_category.category}', so it was treated as a category."
            )
            category_term, merchant_term = merchant_term, None

    if category_term:
        result = _apply_category(result, category_term)
    if merchant_term:
        result = _apply_merchant(result, merchant_term)

    if (category_term or merchant_term) and not result.transactions:
        result.filtered_to_nothing = True
    return result


def _apply_category(result: ResolvedFilters, term: str) -> ResolvedFilters:
    match: CategoryMatch | None = resolve_category(term, _available_categories(result.transactions))

    if match is None:
        # Absent from THIS period is not the same as unrecognised. Fall back
        # to the service's own vocabulary: "groceries" in a week with no
        # grocery shopping is a genuine zero and must be answered as one,
        # while "Category That Does Not Exist" is a question about something
        # that was never a category, and answering that with $0.00 would have
        # the user believe they simply did not spend there.
        known = resolve_category(term, known_category_names())
        result.transactions = []
        if known is not None:
            result.category = known.category
            result.notes.append(
                f"'{term}' is a real spending category but has no transactions in this period. "
                "The total is a genuine zero, not missing data."
            )
        else:
            result.notes.append(
                f"No spending category resembling '{term}' exists in this user's data. This is NOT "
                "a zero total -- say the category was not found, and name what they do have."
            )
        return result

    result.category = match.category
    result.transactions = [t for t in result.transactions if matches_category(t.category, match)]

    wanted = match.subcategory or subcategory_requested(term)
    if wanted:
        result = _apply_subcategory(result, term, wanted)

    # Disclose a loose match only if the scope really did end up wider than
    # the question. When "restaurants" reached the Restaurant rows inside
    # Food And Drink, the answer IS about restaurants -- telling the model it
    # measured something broader would make it hedge a figure that is exact,
    # and _apply_subcategory has already written the note for the cases where
    # the widening was real.
    if match.how in ("fuzzy", "broader") and result.subcategory is None:
        result.notes.append(
            f"'{term}' was matched to the category '{match.category}', which is the closest label "
            "in this user's data. Name the category you actually measured."
        )
    return result


def _apply_subcategory(result: ResolvedFilters, term: str, wanted: str) -> ResolvedFilters:
    """
    Narrow to the finer label the user named -- 'restaurants', not all food.

    Three outcomes, and the difference between them is the whole point:
    the data has the detail and some rows match; it has the detail and none
    match; it does not carry the detail at all. Only the first can be
    answered as asked, and the other two must say which question they
    answered instead.
    """
    has_detail = any(t.subcategory for t in result.transactions)
    if not has_detail:
        result.notes.append(
            f"This user's data has no breakdown finer than '{result.category}', so the figures "
            f"cover ALL {result.category} spending, not '{term}' specifically. Say so plainly."
        )
        return result

    narrowed = [t for t in result.transactions if matches_subcategory(t.subcategory, wanted)]
    if not narrowed:
        result.notes.append(
            f"No '{term}' transactions were recorded under {result.category} in this period, so the "
            f"figures cover ALL {result.category} spending instead. Say which one you are reporting."
        )
        return result

    result.subcategory = wanted
    result.transactions = narrowed
    return result


def _apply_merchant(result: ResolvedFilters, term: str) -> ResolvedFilters:
    matches = find_merchant_matches(result.transactions, term)

    if not matches:
        # The merchant is genuinely absent. Suggestions come from the same
        # transaction set, so every name offered is one the user really has.
        result.suggestions = suggest_merchants(result.transactions, term)
        result.transactions = []
        if result.suggestions:
            result.notes.append(
                f"No merchant matching '{term}' appears in this period. The closest names in the "
                f"user's own transactions are: {', '.join(result.suggestions)}. Offer these rather "
                "than reporting a total of zero."
            )
        else:
            result.notes.append(
                f"No merchant matching '{term}' appears in this period, and nothing in the data "
                "is close to it. Say the merchant was not found -- do NOT report zero spending, "
                "which would imply they have the account and simply did not use it."
            )
        return result

    result.merchant_matches = matches
    result.merchants = [m.display_name for m in matches]
    result.transactions = [t for m in matches for t in m.transactions]
    return result
