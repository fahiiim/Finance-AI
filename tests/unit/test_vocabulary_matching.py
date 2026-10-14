"""
The user's words against the database's labels.

Every failure guarded here had the same shape in production: a question the
assistant appeared to answer, with a number that was wrong or meaningless,
and nothing in the reply to show it. "$0.00 on restaurants" is dangerous
because it reads exactly like a real answer.

  - "restaurants" had to find "Food And Drink" (Plaid's label), and
    "Wendy's" had to find "WENDY S 3319" (what the card network prints).
    Both were exact string comparisons, so both matched nothing.
  - A phrase in the wrong slot -- the planner guessing "merchant" for a
    category word, or the reverse -- produced the same silent zero.
  - A merchant that genuinely is not in the data must be REPORTED as absent,
    not totalled to zero: the user cannot tell those apart otherwise.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.schemas.financial import Transaction
from app.tools.calculations import calculate_category_total, calculate_merchant_total
from app.tools.filters import apply_filters
from app.utils.categories import resolve_category, subcategory_requested
from app.utils.merchants import (
    clean_merchant_name,
    find_merchant_matches,
    suggest_merchants,
    top_merchants,
)

# The vocabularies of the two backends this service supports. They share no
# labels, which is the point: resolution must work against the data in hand.
PLAID_LABELS = ["Food And Drink", "General Merchandise", "Rent And Utilities", "Transportation", "Medical"]
SAMPLE_LABELS = ["Groceries", "Dining", "Shopping", "Utilities", "Transportation", "Subscriptions"]


def _txn(merchant: str, amount: str, *, category="Food And Drink", subcategory=None, day=5) -> Transaction:
    return Transaction(
        transaction_id=f"{merchant}-{day}-{amount}",
        account_id="acct",
        amount=Decimal(amount),
        date=date(2026, 7, day),
        merchant_name=merchant,
        category=category,
        subcategory=subcategory,
    )


# Descriptors copied verbatim from production data.
REAL_TRANSACTIONS = [
    _txn("CHECKCARD 0607 CHEESECAKE SUMMERLIN LAS VEGAS NV XXXXX0061XXXXXXXXXX7485", "81.80",
         subcategory="Restaurant", day=7),
    _txn("CHECKCARD 0614 CHEESECAKE SUMMERLIN LAS VEGAS NV XXXXX0061XXXXXXXXXX8327", "52.33",
         subcategory="Restaurant", day=14),
    _txn("CHECKCARD 0701 WENDY S 3319", "7.15", subcategory="Fast Food", day=1),
    _txn("CHECKCARD 0531 SMITHS-FOOD #4349 LAS VEGAS NV XXXXX0061XXXXXXXXXX8386", "19.79",
         subcategory="Groceries", day=2),
    _txn("SMITHS-FOOD #4 06/02 #XXXXX7940 MOBILE PURCHASE SMITHS-FOOD #4349", "37.82",
         subcategory="Groceries", day=3),
    _txn("CHECKCARD 0605 KFC D212094 LAS VEGAS NV XXXXX0061XXXXXXXXXX4640", "7.68",
         subcategory="Fast Food", day=5),
    _txn("Walgreens", "24.10", category="Medical", subcategory="Pharmacies And Supplements", day=9),
]


# --------------------------------------------------------------- categories


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("restaurant", "Food And Drink"),
        ("restaurants", "Food And Drink"),
        ("Restaurants", "Food And Drink"),
        ("eating out", "Food And Drink"),
        ("dining", "Food And Drink"),
        ("groceries", "Food And Drink"),
        ("coffee", "Food And Drink"),
        ("food and drinks", "Food And Drink"),
        ("food & drink", "Food And Drink"),
        ("gas", "Transportation"),
        ("fuel", "Transportation"),
        ("rent", "Rent And Utilities"),
        ("shopping", "General Merchandise"),
        ("health", "Medical"),
    ],
)
def test_everyday_words_reach_plaid_labels(phrase, expected):
    """The reported bug: 'restaurant' returned nothing while 'food and drinks' worked."""
    match = resolve_category(phrase, PLAID_LABELS)
    assert match is not None, f"{phrase!r} resolved to nothing"
    assert match.category == expected


@pytest.mark.parametrize(
    "phrase,expected",
    [("restaurants", "Dining"), ("eating out", "Dining"), ("groceries", "Groceries"),
     ("shopping", "Shopping"), ("subscriptions", "Subscriptions"), ("gas", "Transportation")],
)
def test_the_same_words_reach_a_different_backends_labels(phrase, expected):
    """Resolution is against the data in hand, not a hard-coded taxonomy."""
    match = resolve_category(phrase, SAMPLE_LABELS)
    assert match is not None and match.category == expected


def test_a_narrower_word_reports_what_it_wanted():
    """'restaurants' is narrower than the label it matched, and must say so."""
    match = resolve_category("restaurants", PLAID_LABELS)
    assert match.subcategory == "Restaurant"
    assert match.is_broader_than_asked

    # ...but a word that IS the label is not narrower than anything.
    assert resolve_category("food and drinks", PLAID_LABELS).subcategory is None
    assert resolve_category("dining", SAMPLE_LABELS).subcategory is None


def test_a_merchant_name_is_not_mistaken_for_a_category():
    """Otherwise every shop name would resolve to whichever label it looked like."""
    for name in ("glato", "netflix", "Walgreens", "Cheesecake Summerlin"):
        assert resolve_category(name, PLAID_LABELS) is None, name


def test_subcategory_requested_only_reports_genuinely_finer_concepts():
    assert subcategory_requested("restaurants") == "Restaurant"
    assert subcategory_requested("takeout") == "Fast Food"
    assert subcategory_requested("food and drink") is None
    assert subcategory_requested("transportation") is None


# ---------------------------------------------------------------- merchants


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CHECKCARD 0607 CHEESECAKE SUMMERLIN LAS VEGAS NV XXXXX0061XXXXXXXXXX7485", "Cheesecake Summerlin"),
        ("CHECKCARD 0605 KFC D212094 LAS VEGAS NV XXXXX0061XXXXXXXXXX4640", "KFC"),
        ("CHECKCARD 0704 SMITHS-FOOD #4", "Smiths-Food"),
        ("SMITHS-FOOD #4 06/02 #XXXXX7940 MOBILE PURCHASE SMITHS-FOOD #4349", "Smiths-Food"),
        ("CHECKCARD 0307 VELOCITY ESPORTS LOS ANGELES CA XXXXX0660XXXXXXXXXX0206", "Velocity Esports"),
        # A two-word name must never be trimmed to its first word.
        ("PANDA EXPRESS CA", "Panda Express"),
        # Casing the source already carries is left alone.
        ("Chick-fil-A", "Chick-fil-A"),
        ("www.fiverr.com", "www.fiverr.com"),
        # Acronyms stay shouted; 'Cvs' is not a brand.
        ("CVS", "CVS"),
    ],
)
def test_statement_descriptors_become_names_a_person_would_type(raw, expected):
    assert clean_merchant_name(raw) == expected


def test_an_ach_descriptor_does_not_leak_a_third_partys_name():
    """
    These strings carry the RECIPIENT's full legal name after INDN:. It reached
    subscription and bill lists verbatim, putting one person's name in front of
    another as the name of their subscription.
    """
    cleaned = clean_merchant_name(
        "NEW AMERICAN FND DES:HOME LOAN ID:XXXXX00000 INDN:JANE ALEX DOE CO ID:00000 PPD"
    )
    assert "JANE" not in cleaned.upper()
    assert "DOE" not in cleaned.upper()
    assert cleaned.lower().startswith("new american")


@pytest.mark.parametrize("query", ["Wendy's", "wendys", "WENDY S", "wendy"])
def test_a_possessive_finds_a_descriptor_that_dropped_the_apostrophe(query):
    matches = find_merchant_matches(REAL_TRANSACTIONS, query)
    assert matches, f"{query!r} matched nothing"
    assert matches[0].display_name == "Wendy S"


@pytest.mark.parametrize("typo", ["glato", "gelato", "Gelatto", "gelatoo", "Gelatos"])
def test_a_misspelled_merchant_is_still_found(typo):
    """
    The reported bug, exactly: a shop was named in a list the assistant had
    just shown, the user typed it back approximately, and the answer was zero.
    A typo is the normal case, not the exceptional one.
    """
    gelato = [_txn("Gelato Bar & Restaurant", "12.50")]
    matches = find_merchant_matches(gelato, typo)
    assert matches, f"{typo!r} matched nothing"
    assert matches[0].transactions[0].amount == Decimal("12.50")


def test_every_phrasing_of_one_merchant_gives_one_total():
    """
    Tiering once ranked exact matches above phrase matches, so "Smith's"
    totalled every row and "smiths food" totalled only the handful that
    happened to tokenize exactly -- two answers to the same question.
    """
    totals = {
        query: sum(len(m.transactions) for m in find_merchant_matches(REAL_TRANSACTIONS, query))
        for query in ("Smith's", "smiths", "smiths food", "SMITHS-FOOD")
    }
    assert len(set(totals.values())) == 1, totals
    assert set(totals.values()) == {2}


def test_repeat_visits_collapse_into_one_named_row():
    """Two descriptors, one shop. A list of statement lines is not a merchant list."""
    matches = find_merchant_matches(REAL_TRANSACTIONS, "cheesecake")
    assert [m.display_name for m in matches] == ["Cheesecake Summerlin"]
    assert len(matches[0].transactions) == 2
    assert len(matches[0].raw_names) == 2


def test_a_missing_merchant_is_absent_not_zero():
    resolved = apply_filters(REAL_TRANSACTIONS, merchant_filter="Starbucks")
    assert resolved.transactions == []
    assert resolved.filtered_to_nothing
    notes = " ".join(resolved.notes)
    assert "not found" in notes or "No merchant matching" in notes
    assert "do NOT report zero" in notes


def test_a_different_branch_of_a_similar_name_is_offered_not_totalled():
    """
    'Cheesecake Factory' is not 'Cheesecake Summerlin'. Close enough to name,
    too different to silently total -- so the answer offers it and lets the
    user decide, rather than reporting one shop's spending as another's.
    """
    resolved = apply_filters(REAL_TRANSACTIONS, merchant_filter="Cheesecake Factory")
    assert resolved.transactions == []
    assert "Cheesecake Summerlin" in resolved.suggestions
    assert "Cheesecake Summerlin" in " ".join(resolved.notes)


def test_unrelated_names_are_never_offered_as_suggestions():
    """A wrong 'did you mean?' reads as guessing, which is worse than silence."""
    assert suggest_merchants(REAL_TRANSACTIONS, "Starbucks") == []


# ------------------------------------------------------------- slot swapping


def test_a_category_word_in_the_merchant_slot_still_works():
    resolved = apply_filters(REAL_TRANSACTIONS, merchant_filter="restaurants")
    assert resolved.category == "Food And Drink"
    assert resolved.subcategory == "Restaurant"
    assert resolved.transactions, "the planner's slot choice must not decide the answer"
    assert "treated as a category" in " ".join(resolved.notes)


def test_a_merchant_name_in_the_category_slot_still_works():
    resolved = apply_filters(REAL_TRANSACTIONS, category_filter="Walgreens")
    assert resolved.merchants == ["Walgreens"]
    assert len(resolved.transactions) == 1
    assert "treated as a merchant name" in " ".join(resolved.notes)


# ------------------------------------------------------------- subcategories


def test_restaurants_narrows_within_food_and_drink_when_the_data_allows():
    resolved = apply_filters(REAL_TRANSACTIONS, category_filter="restaurants")
    assert resolved.category == "Food And Drink"
    assert resolved.subcategory == "Restaurant"
    assert {t.subcategory for t in resolved.transactions} == {"Restaurant"}
    total = sum(t.amount for t in resolved.transactions)
    assert total == Decimal("134.13"), "only the two restaurant rows"


def test_food_and_drink_covers_every_subcategory():
    resolved = apply_filters(REAL_TRANSACTIONS, category_filter="food and drinks")
    assert resolved.subcategory is None
    assert len(resolved.transactions) == 6, "restaurants, fast food and groceries alike"


def test_a_narrow_question_says_so_when_the_data_cannot_answer_it():
    """
    Without subcategories the only honest answer is the parent's total, and
    the answer must SAY it widened -- otherwise all food spending is reported
    as restaurant spending, and the arithmetic hides it.
    """
    flat = [_txn("Some Cafe", "10.00", subcategory=None)]
    resolved = apply_filters(flat, category_filter="restaurants")
    assert resolved.subcategory is None
    assert resolved.transactions == flat
    assert "no breakdown finer than" in " ".join(resolved.notes)


def test_no_spurious_widening_note_when_the_narrowing_succeeded():
    """A hedge on an exact figure teaches the user to distrust exact figures."""
    resolved = apply_filters(REAL_TRANSACTIONS, category_filter="restaurants")
    assert "closest label" not in " ".join(resolved.notes)


# ------------------------------------------------- the calculation engine


def test_deterministic_totals_use_the_same_matching():
    """
    Two totals for one question is worse than one wrong total. These helpers
    are reachable independently of the tools, so they must not disagree.
    """
    assert calculate_category_total(REAL_TRANSACTIONS, "restaurants") == Decimal("134.13")
    assert calculate_merchant_total(REAL_TRANSACTIONS, "Wendy's") == Decimal("7.15")
    assert calculate_merchant_total(REAL_TRANSACTIONS, "Smith's") == Decimal("57.61")


def test_top_merchants_reports_cleaned_grouped_names():
    ranked = top_merchants(REAL_TRANSACTIONS)
    names = [row["merchant"] for row in ranked]
    assert "Smiths-Food" in names and "Cheesecake Summerlin" in names
    assert not any("CHECKCARD" in name for name in names)
    smiths = next(row for row in ranked if row["merchant"] == "Smiths-Food")
    assert smiths["count"] == 2, "both descriptors are the same shop"


def test_spelling_variants_of_one_shop_are_one_row():
    """
    A merchant list is a list of shops, not of statement lines. 'SMITHS-FOOD'
    and 'SMITHS FOOD' differ by a hyphen the bank inserted inconsistently;
    shown as two rows they read as two businesses, and each carries half the
    real total.
    """
    variants = [
        _txn("CHECKCARD 0321 SMITHS-FOOD #4349 LAS VEGAS NV XXXXX0060XXXXX", "10.00", day=1),
        _txn("SMITHS FOOD #4 02/02 #XXXXX2969 PURCHASE SMITHS FOOD #4347", "20.00", day=2),
        _txn("MOBILE PURCHASE 0324 SMITHS-FOOD #4349 LAS VEGAS NV XXXXX006", "30.00", day=3),
    ]
    ranked = top_merchants(variants)
    assert len(ranked) == 1, ranked
    assert ranked[0]["count"] == 3
    assert Decimal(ranked[0]["total"]) == Decimal("60.00")

    matches = find_merchant_matches(variants, "Smiths")
    assert len(matches) == 1
    assert len(matches[0].transactions) == 3


def test_a_channel_prefix_does_not_leave_the_date_as_the_name():
    """'MOBILE PURCHASE 0324 SMITHS-FOOD' must not display as '0324 Smiths-Food'."""
    assert clean_merchant_name(
        "MOBILE PURCHASE 0324 SMITHS-FOOD #4349 LAS VEGAS NV XXXXX006"
    ) == "Smiths-Food"
