"""
Category vocabulary resolution.

THE PROBLEM THIS SOLVES
    The user says "restaurants". The database says "Food And Drink". Every
    category filter in this codebase used to be `t.category.lower() ==
    filter.lower()`, so those two never met: the question returned zero rows
    and the assistant reported "$0 on restaurants" -- a confident, wrong
    number, in the one shape a user cannot tell apart from a real answer.

    The vocabulary gap is not a bug in one place. It is structural. The
    labels come from Plaid's PERSONAL_FINANCE_CATEGORY taxonomy
    (FOOD_AND_DRINK, RENT_AND_UTILITIES, GENERAL_MERCHANDISE), prettified for
    display; people ask about "eating out", "rent", "shopping". No prompt
    instruction fixes that, because the model cannot know which labels a
    given user's data actually contains.

HOW IT WORKS
    resolve_category() scores the user's phrase against the labels that are
    ACTUALLY PRESENT in the data being filtered, and returns the best one
    above a threshold. Nothing is hard-coded to one backend's vocabulary:
    the same call resolves "dining" to "Dining" against a test client and
    to "Food And Drink" against the live Plaid-backed one.

SUBCATEGORIES
    Plaid's detailed level distinguishes FOOD_AND_DRINK_RESTAURANT from
    FOOD_AND_DRINK_GROCERIES, and "how much on restaurants?" means the first
    one. When the query names a fine concept the data cannot express, the
    match still succeeds at the parent level and reports the subcategory it
    WANTED -- so the caller can narrow if subcategory data is available and
    say plainly that it answered more broadly if it is not. Silently widening
    "restaurants" to all food spending, with nothing in the answer to show
    it, is the failure this field exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

# A phrase must score at least this well against a real category label before
# we will filter on it. Below this we return no match, and the caller falls
# back (often to treating the phrase as a merchant name instead) rather than
# reporting a zero total for a category the user never had.
MATCH_THRESHOLD = 0.75


@dataclass(frozen=True)
class CategoryMatch:
    """A user's phrase, resolved against the labels present in their data."""

    #: The category label exactly as it appears in the data, ready to filter on.
    category: str
    #: The finer concept the user actually named ("Restaurant" for "eating
    #: out"), when it is narrower than `category`. None when the phrase and
    #: the label mean the same thing.
    subcategory: str | None
    #: exact | label | concept | broader | fuzzy -- reported so the answer can
    #: disclose a loose match instead of presenting it as what was asked.
    how: str
    score: float

    @property
    def is_broader_than_asked(self) -> bool:
        """True when we matched a parent category for a narrower question."""
        return self.subcategory is not None


# Fine concepts. Each maps a set of phrases people actually type to a single
# idea. The dict key is the canonical name used when reporting a subcategory
# back to the user.
_FINE_CONCEPTS: dict[str, tuple[str, ...]] = {
    "Restaurant": (
        "restaurant", "restaurants", "dining", "dine", "dining out", "eating out", "eat out",
        "eats", "meals out", "sit down", "diner", "diners", "food out", "ate out", "brunch",
    ),
    "Groceries": (
        "groceries", "grocery", "grocery store", "supermarket", "supermarkets", "food shopping",
        "grocery shopping", "market", "produce",
    ),
    "Coffee": ("coffee", "coffee shop", "coffee shops", "cafe", "cafes", "espresso", "tea shop"),
    "Fast Food": (
        "fast food", "takeout", "take out", "take away", "takeaway", "drive thru", "drive through",
        "food delivery", "burgers", "pizza",
    ),
    "Alcohol": (
        "alcohol", "liquor", "beer", "wine", "bar", "bars", "pub", "pubs", "drinking",
        "beer wine and liquor", "booze", "nightlife",
    ),
    "Food And Drink": (
        "food and drink", "food and drinks", "food", "foods", "drink", "drinks",
        "food and beverage", "meals", "meal", "snacks", "dining and food",
    ),
    "Transportation": (
        "transportation", "transport", "commute", "commuting", "rideshare", "ride share",
        "uber", "lyft", "taxi", "taxis", "cab", "transit", "public transit", "bus", "train",
        "parking", "tolls", "car", "auto", "vehicle", "gas", "fuel", "petrol", "gasoline",
    ),
    "Travel": (
        "travel", "trip", "trips", "vacation", "vacations", "holiday", "holidays", "flight",
        "flights", "airfare", "airlines", "hotel", "hotels", "lodging", "accommodation",
    ),
    "General Merchandise": (
        "general merchandise", "merchandise", "shopping", "shop", "retail", "stores", "store",
        "clothes", "clothing", "apparel", "electronics", "household", "goods", "online shopping",
    ),
    "Sporting Goods": ("sporting goods", "sports", "sports gear", "outdoor gear", "athletics"),
    "Rent And Utilities": (
        "rent and utilities", "rent", "utilities", "utility", "housing", "house", "mortgage",
        "electric", "electricity", "power bill", "water bill", "gas bill", "internet",
        "phone bill", "cable", "bills", "living costs", "accommodation costs",
    ),
    "Entertainment": (
        "entertainment", "fun", "movies", "cinema", "streaming", "games", "gaming", "concerts",
        "hobbies", "leisure", "shows", "events",
    ),
    "Medical": (
        "medical", "health", "healthcare", "health care", "doctor", "doctors", "pharmacy",
        "medicine", "prescriptions", "dental", "dentist", "hospital", "vision", "therapy",
    ),
    "Personal Care": (
        "personal care", "grooming", "haircut", "hair", "salon", "spa", "beauty", "cosmetics",
        "gym", "fitness", "wellness", "barber",
    ),
    "General Services": (
        "general services", "services", "service", "professional services", "repairs",
        "maintenance", "cleaning", "legal", "accounting", "software",
    ),
    "Subscriptions": (
        "subscriptions", "subscription", "memberships", "membership", "recurring",
        "recurring charges", "streaming services",
    ),
    "Loan Payments": (
        "loan payments", "loan payment", "loans", "loan", "debt", "debts", "debt payments",
        "repayments", "car payment", "student loan", "student loans", "credit card payment",
        "mortgage payment",
    ),
    "Bank Fees": (
        "bank fees", "fees", "fee", "charges", "bank charges", "overdraft", "interest charges",
        "atm fees", "service charges",
    ),
    "Home Improvement": (
        "home improvement", "home repairs", "renovation", "renovations", "furniture", "hardware",
        "diy", "yard", "garden",
    ),
    "Government And Non Profit": (
        "government and non profit", "government", "taxes", "tax", "charity", "charities",
        "donations", "donation", "non profit", "nonprofit", "civic",
    ),
    "Income": (
        "income", "salary", "salary and wages", "wages", "paycheck",
        "paychecks", "pay", "earnings", "revenue", "deposits",
    ),
    "Transfer": (
        "transfer", "transfers", "transfer in", "transfer out", "moved money", "internal transfer",
    ),
    "Other": ("other", "miscellaneous", "misc", "uncategorized", "uncategorised"),
}

# Which fine concepts live under which parent. A question about a fine
# concept can be answered from its parent's rows when the data has no finer
# breakdown -- flagged, never silently.
_PARENTS: dict[str, str] = {
    "Restaurant": "Food And Drink",
    "Groceries": "Food And Drink",
    "Coffee": "Food And Drink",
    "Fast Food": "Food And Drink",
    "Alcohol": "Food And Drink",
    "Food And Drink": "Food And Drink",
    "Sporting Goods": "General Merchandise",
    "General Merchandise": "General Merchandise",
    "Transportation": "Transportation",
    "Travel": "Travel",
    "Rent And Utilities": "Rent And Utilities",
    "Entertainment": "Entertainment",
    "Medical": "Medical",
    "Personal Care": "Personal Care",
    "General Services": "General Services",
    "Subscriptions": "Subscriptions",
    "Loan Payments": "Loan Payments",
    "Bank Fees": "Bank Fees",
    "Home Improvement": "Home Improvement",
    "Government And Non Profit": "Government And Non Profit",
    "Income": "Income",
    "Transfer": "Transfer",
    "Other": "Other",
}


def _singularize(token: str) -> str:
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalize_category(text: str) -> str:
    """'Food & Drinks' -> 'food and drink'. The form everything is compared in."""
    lowered = text.lower().replace("&", " and ")
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in lowered)
    return " ".join(_singularize(t) for t in cleaned.split())


# Built once: normalized alias -> fine concept, longest alias first at lookup
# time so "gas bill" is a utility while "gas" alone is fuel.
_ALIAS_TO_CONCEPT: dict[str, str] = {}
for _concept, _aliases in _FINE_CONCEPTS.items():
    for _alias in _aliases:
        _ALIAS_TO_CONCEPT.setdefault(normalize_category(_alias), _concept)


def _contains_phrase(haystack: str, needle: str) -> bool:
    return f" {needle} " in f" {haystack} "


def _concept_of(text: str) -> str | None:
    """The fine concept a phrase names, matching the longest alias first."""
    normalized = normalize_category(text)
    if not normalized:
        return None
    exact = _ALIAS_TO_CONCEPT.get(normalized)
    if exact:
        return exact
    best: tuple[int, str] | None = None
    for alias, concept in _ALIAS_TO_CONCEPT.items():
        # Whole-phrase containment only, so "car" does not fire inside "care".
        if _contains_phrase(normalized, alias) and (best is None or len(alias) > best[0]):
            best = (len(alias), concept)
    return best[1] if best else None


def _score(query: str, label: str) -> tuple[float, str]:
    """How well a user's phrase matches one real category label."""
    q, lab = normalize_category(query), normalize_category(label)
    if not q or not lab:
        return 0.0, "none"
    if q == lab:
        return 1.0, "exact"
    if _contains_phrase(lab, q) or _contains_phrase(q, lab):
        return 0.92, "label"

    q_concept, label_concept = _concept_of(query), _concept_of(label)
    if q_concept and label_concept:
        if q_concept == label_concept:
            return 0.90, "concept"
        if _PARENTS.get(q_concept) == _PARENTS.get(label_concept):
            # "restaurants" against a "Food And Drink" label: the right rows,
            # a broader question than the one asked. Flagged as such.
            return 0.80, "broader"

    ratio = SequenceMatcher(None, q, lab).ratio()
    return (ratio * 0.88, "fuzzy") if ratio >= 0.80 else (0.0, "none")


def resolve_category(query: str | None, available: list[str] | set[str]) -> CategoryMatch | None:
    """
    Resolve a user's category phrase against the labels present in their data.

    `available` is the categories actually found in the transactions being
    filtered -- not a fixed list -- so this works unchanged against any
    backend's vocabulary. Returns None when nothing scores well enough, which
    is the caller's signal to try the phrase as a merchant instead of
    reporting a zero total for a category the user does not have.
    """
    if not query:
        return None

    best: CategoryMatch | None = None
    for label in sorted({c for c in available if c}):
        score, how = _score(query, label)
        if score < MATCH_THRESHOLD or (best is not None and score <= best.score):
            continue
        subcategory = None
        if how in ("broader", "concept"):
            # Only a genuinely FINE concept counts. "food and drinks" matching
            # a "Dining" column is a loose match, not a narrower one, and
            # calling it a subcategory would have the answer promise a
            # breakdown that is really the whole figure.
            concept = subcategory_requested(query)
            if concept and normalize_category(concept) != normalize_category(label):
                subcategory = concept
        best = CategoryMatch(category=label, subcategory=subcategory, how=how, score=score)
    return best


def known_category_names() -> list[str]:
    """
    Every category this service recognises, independent of any user's data.

    The fallback vocabulary for a phrase that matches nothing in the period
    being looked at. "How much on groceries last week?" when they bought none
    must answer "nothing" -- resolving only against the period's own labels
    would answer "you have no such category", which is false and unfixable by
    the user.
    """
    return sorted(set(_PARENTS.values()) | set(_FINE_CONCEPTS))


def subcategory_requested(query: str | None) -> str | None:
    """
    The fine concept a phrase names, independent of any data.

    Used when the data DOES carry subcategories: "restaurants" then narrows
    to the restaurant rows rather than settling for the parent category.
    """
    if not query:
        return None
    concept = _concept_of(query)
    if concept is None or _PARENTS.get(concept) == concept:
        return None
    return concept


def matches_category(transaction_category: str | None, match: CategoryMatch) -> bool:
    """Whether one transaction's category belongs to a resolved match."""
    if not transaction_category:
        return False
    return normalize_category(transaction_category) == normalize_category(match.category)


def matches_subcategory(transaction_subcategory: str | None, subcategory: str) -> bool:
    """
    Whether a transaction's fine label is the one asked for.

    Compared through the concept table rather than by string, so Plaid's
    'Fast Food' and a query for 'takeout' land on the same rows.
    """
    if not transaction_subcategory:
        return False
    a, b = normalize_category(transaction_subcategory), normalize_category(subcategory)
    if a == b or _contains_phrase(a, b) or _contains_phrase(b, a):
        return True
    concept_a, concept_b = _concept_of(transaction_subcategory), _concept_of(subcategory)
    return bool(concept_a and concept_a == concept_b)
