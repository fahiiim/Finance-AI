"""
Merchant name cleaning and matching.

WHY THIS EXISTS
    Merchant matching was `LOWER(name) LIKE '%query%'` -- a raw substring
    test against whatever the card network happened to print on the
    statement. Against the real data those strings look like this:

        CHECKCARD 0607 CHEESECAKE SUMMERLIN LAS VEGAS NV XXXXX0061XXXXXXXXXX7485
        CHECKCARD 0701 WENDY S 3319
        CHECKCARD 0704 SMITHS-FOOD #4
        STATE UI PAYMNT DES:UI BENEFIT ID:00000000 INDN:DOE, JANE CO ID:SXXXXX0000 PPD
        TST*ORIZABAS MEXICAN G Las Vegas NV XXXXX1661XXXXXXXXXX0068

    So "how much did I spend at Wendy's?" matched nothing (the statement says
    "WENDY S"), "Smith's" matched nothing ("SMITHS-FOOD"), and "Cheesecake
    Factory" matched nothing ("CHEESECAKE SUMMERLIN"). Each of those returned
    a $0 total that reads exactly like a real answer.

    Worse, the same shop appears under dozens of distinct strings -- one per
    transaction, because the date and card mask are baked into the name -- so
    a merchant list showed twenty rows of noise where a person expects one
    row saying "Smiths-Food, 11 visits".

TWO JOBS, DELIBERATELY SEPARATE
    clean_merchant_name() is for DISPLAY and GROUPING: it strips the payment
    rails' scaffolding so the user sees "Cheesecake Summerlin", and so
    repeated visits collapse into one row. What the user is shown is then a
    name they can actually type back as a follow-up question.

    find_merchant_matches() is for MATCHING: it works on token sets, not
    substrings, and tiers strictly -- an exact match never gets diluted by
    fuzzy noise, and fuzzy only runs when nothing better matched at all.

    suggest_merchants() covers the remaining case honestly: nothing matched,
    so name the closest things the user actually spent money at instead of
    reporting zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.schemas.financial import Transaction

# Payment-rail scaffolding: present on the statement, never part of the name.
_NOISE_TOKENS = frozenset({
    "checkcard", "check", "card", "pos", "purchase", "debit", "credit", "recurring",
    "payment", "pmt", "paymnt", "ach", "eft", "withdrawal", "wthdrwl", "des", "id",
    "indn", "co", "ppd", "web", "tel", "arc", "ccd", "ref", "trn", "seq", "auth",
    "transaction", "sale", "visa", "mastercard", "amex", "discover", "xxxxx",
    "llc", "inc", "ltd", "corp", "company", "the", "and", "of", "at", "on", "to",
    "www", "com", "net", "org", "http", "https",
})

# Two-letter US state codes strip cleanly off the tail of a card descriptor.
# They are never the merchant, and leaving them in makes "CA" and "IN" behave
# like name tokens during matching.
_STATE_CODES = frozenset({
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv",
    "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn",
    "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
})

# Anything from `DES:` onward is ACH routing detail -- the originator's name,
# an internal id, and the recipient's own name. Keeping it leaks one user's
# name into another's merchant list and buries the payer under 60 characters
# of reference numbers.
_ACH_TAIL = re.compile(r"\s+(?:DES|ID|INDN|CO ID|REF|TRN)\s*:.*$", re.IGNORECASE)
# Card masks: XXXXX0061XXXXXXXXXX7485, XXX-XX11160, ************1234.
_CARD_MASK = re.compile(r"\b[X*]{3,}[\dX*\-]*\b", re.IGNORECASE)
# A leading transaction type followed by the MMDD of the charge.
_LEADING_TYPE = re.compile(
    r"^\s*(?:CHECKCARD|CHECK CARD|POS DEBIT|POS|PURCHASE|DEBIT CARD|DEBIT|CREDIT|"
    r"RECURRING|ACH|EFT|VISA|SALE)\s+(?:\d{4}\s+)?",
    re.IGNORECASE,
)
# Aggregator prefixes glued to the front with an asterisk: TST*, SQ *, PP*.
_AGGREGATOR = re.compile(r"^\s*(?:TST|SQ|SP|PP|PAYPAL|IN|WWW|CKE)\s*\*\s*", re.IGNORECASE)
# Store numbers: "#3368", "D212094", a bare 3+ digit run at the end.
_STORE_NUMBER = re.compile(r"\s*#\s*\d+\b|\s+\b[A-Z]?\d{3,}\b\s*$", re.IGNORECASE)
# The posting date, printed inside the descriptor: 'SMITHS-FOOD 06/02 ...',
# 'LITTLE CAESARS ... ON 06/12'.
_INLINE_DATE = re.compile(r"\s*\b(?:ON\s+)?\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", re.IGNORECASE)
# Channel wording the bank inserts between the merchant and its repeat.
_CHANNEL = re.compile(
    r"\s*\b(?:MOBILE PURCHASE|ONLINE PURCHASE|RECURRING PAYMENT|POS PURCHASE|"
    r"PURCHASE AUTHORIZED ON|CARD PURCHASE|PREAUTHORIZED)\b\s*",
    re.IGNORECASE,
)
# A bare 'PURCHASE' sitting BETWEEN two things, which is the separator in
# 'SMITHS-FOOD ... PURCHASE SMITHS-FOOD'. Mid-string only: leading-position
# 'PURCHASE' is already handled as a transaction type, and stripping the word
# anywhere would turn a merchant genuinely called 'Purchase Power' into
# 'Power'.
_MID_PURCHASE = re.compile(r"(?<=\w)\s+PURCHASE\s+(?=\w)", re.IGNORECASE)
# The MMDD left at the front once a channel prefix is removed:
# 'MOBILE PURCHASE 0324 SMITHS-FOOD ...' -> '0324 SMITHS-FOOD ...'. Only a
# leading bare four-digit group, so a merchant whose name starts with a year
# ('1800 Flowers') keeps it.
_LEADING_DATE = re.compile(r"^\s*\d{4}\s+(?=\D)")
# A '#' orphaned by removing the masked reference it introduced.
_BARE_HASH = re.compile(r"\s*#(?!\w)")
_TRAILING_STATE = re.compile(r"\s+\b(" + "|".join(sorted(_STATE_CODES)) + r")\b\s*$", re.IGNORECASE)

# Longer than this and a "merchant name" is a statement descriptor, not
# something a person would recognise or type back.
MAX_DISPLAY_TOKENS = 6

# Words a truncated descriptor must not end on.
_TRAILING_STOPWORDS = frozenset({"of", "at", "on", "to", "the", "and", "for", "in", "a"})

# Whole-string similarity at which two names are the same shop spelled
# differently. "glato" vs "gelato" scores 0.91; unrelated names sit far below.
FUZZY_WHOLE = 0.82
# Per-token similarity, held higher: single tokens are short, and short
# strings score deceptively well against each other.
FUZZY_TOKEN = 0.85
# A multi-word query need not match every word -- "Cheesecake Factory"
# against "CHEESECAKE SUMMERLIN" is the shop the user means.
MIN_TOKEN_COVERAGE = 0.6
# Similarity below which a "did you mean?" is noise rather than a lead.
SUGGEST_THRESHOLD = 0.7


@dataclass
class MerchantMatch:
    """One real merchant, with every statement string that turned out to be it."""

    display_name: str
    transactions: list[Transaction] = field(default_factory=list)
    raw_names: set[str] = field(default_factory=set)


def merchant_key(name: str) -> str:
    """The form two spellings of one shop have in common."""
    return " ".join(_tokens(name))


def _preferred_display(raw_names: set[str]) -> str:
    """
    The most readable spelling among the descriptors for one shop.

    Shortest first, because the shortest cleaned descriptor is the one with
    the least surviving scaffolding; ties break alphabetically so the label is
    stable across requests rather than following set iteration order.
    """
    cleaned = {clean_merchant_name(raw) for raw in raw_names}
    return min(cleaned, key=lambda name: (len(name), name))


def _dedupe_repeat(text: str) -> str:
    """
    'SMITHS-FOOD SMITHS-FOOD' -> 'SMITHS-FOOD'.

    Banks print the merchant twice on mobile-purchase descriptors -- once as
    the header, once after the channel wording. With the channel stripped the
    repeat sits flush against the original.
    """
    tokens = text.split()
    half = len(tokens) // 2
    for size in range(half, 0, -1):
        if tokens[:size] and tokens[size : size * 2] == tokens[:size]:
            return " ".join(tokens[:size] + tokens[size * 2 :])
    return text


def _strip_trailing_city(text: str) -> str:
    """
    Drop a trailing city from a descriptor whose state code was just removed.

    Tries the longest plausible city first ('Las Vegas' before 'Vegas'), and
    how much must survive depends on how much is taken -- see below.
    """
    tokens = text.split()
    # A two-word city may take the name down to a single word -- 'SMITHS-FOOD
    # LAS VEGAS' is one merchant and two location words. A one-word tail must
    # leave two behind, because there is no way to tell the city in 'PANDA
    # EXPRESS CA' from the second half of the merchant's own name, and
    # 'Panda' is a worse answer than 'Panda Express'.
    for city_length, min_remaining in ((2, 1), (1, 2)):
        if len(tokens) - city_length < min_remaining:
            continue
        tail = tokens[-city_length:]
        if all(t.isalpha() for t in tail):
            return " ".join(tokens[:-city_length])
    return text


def clean_merchant_name(raw: str | None) -> str:
    """
    A statement descriptor reduced to the name a person would recognise.

    'CHECKCARD 0607 CHEESECAKE SUMMERLIN LAS VEGAS NV XXXXX0061XXXXXXXXXX7485'
    becomes 'Cheesecake Summerlin'.
    """
    if not raw or not raw.strip():
        return "Unknown merchant"

    text = _ACH_TAIL.sub("", raw.strip())
    text = _LEADING_TYPE.sub("", text)
    text = _AGGREGATOR.sub("", text)
    text = _CARD_MASK.sub(" ", text)
    text = _INLINE_DATE.sub(" ", text)
    text = _CHANNEL.sub(" ", text)
    text = _STORE_NUMBER.sub("", text)
    text = _BARE_HASH.sub("", text)
    text = _MID_PURCHASE.sub(" ", text)
    text = _LEADING_DATE.sub("", text.strip())
    text = _dedupe_repeat(text)

    # 'CHEESECAKE SUMMERLIN LAS VEGAS NV' -> 'CHEESECAKE SUMMERLIN'. The state
    # code is the tell: a card descriptor ends 'CITY ST', so once a state is
    # stripped the words before it are the city. Guarded on both sides -- only
    # plain alphabetic words are taken, and at least two words must survive --
    # because over-trimming turns 'PANDA EXPRESS CA' into 'PANDA', and a
    # merchant list of first words is worse than one with cities in it.
    if _TRAILING_STATE.search(text):
        text = _TRAILING_STATE.sub("", text)
        text = _strip_trailing_city(text)
        # The store number may only now have reached the end of the string:
        # 'KFC D212094 LAS VEGAS NV' hides it in the middle until the city goes.
        text = _STORE_NUMBER.sub("", text)

    text = re.sub(r"\s+", " ", text).strip(" -*,.")

    if not text:
        # Everything was scaffolding. The original is ugly but true, and an
        # empty merchant name in a list is worse than a long one.
        return raw.strip()

    tokens = text.split()
    if len(tokens) > MAX_DISPLAY_TOKENS:
        tokens = tokens[:MAX_DISPLAY_TOKENS]
    # Truncation can leave a dangling preposition ('... Fee Waiver of').
    while tokens and tokens[-1].lower() in _TRAILING_STOPWORDS:
        tokens.pop()
    if not tokens:
        return raw.strip()

    # Title-case only SHOUTED descriptors. Names that already carry their own
    # casing -- 'Chick-fil-A', 'www.fiverr.com', 'iTunes' -- are left alone,
    # because .title() would damage every one of them. Short all-caps words
    # stay shouted: CVS, KFC and ATM are acronyms, and 'Cvs' is not a brand.
    if text.isupper():
        tokens = [t if len(t) <= 3 else t.title() for t in tokens]
    return " ".join(tokens)


def _tokens(text: str) -> list[str]:
    """Significant, comparable words: no punctuation, no rails, no masks."""
    lowered = re.sub(r"[^a-z0-9]+", " ", text.lower())
    out: list[str] = []
    for token in lowered.split():
        if token in _NOISE_TOKENS or token in _STATE_CODES:
            continue
        if token.isdigit():
            continue
        # Possessives and plural-s: 'wendys' and 'wendy' are one shop.
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        # A lone letter is the wreckage of an apostrophe -- "Smith's" splits
        # into ['smith', 's']. Keeping it made a two-token query out of a
        # one-word name, so the 60% coverage rule could never be met and
        # "Smith's" matched nothing while "Smiths" matched fine.
        if len(token) > 1:
            out.append(token)
    return out


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _token_hit(query_token: str, candidate_tokens: list[str]) -> bool:
    for token in candidate_tokens:
        if token == query_token:
            return True
        # Substring only in the direction that means something: 'smith' inside
        # 'smithsfood' is the same shop; 'a' inside anything is not.
        if len(query_token) >= 4 and query_token in token:
            return True
        if len(query_token) >= 4 and len(token) >= 4 and _similar(query_token, token) >= FUZZY_TOKEN:
            return True
    return False


def _tier(query_tokens: list[str], candidate: str) -> int:
    """
    How strongly one candidate name matches, 4 (exact) down to 0 (not at all).

    Tiers exist so a strong match is never averaged with a weak one. Given
    'Target', tier 4 finds the shop; without tiering, fuzzy matching would
    fold in 'Target Optical' and 'Stargate' and quietly inflate the total.
    """
    candidate_tokens = _tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return 0

    if query_tokens == candidate_tokens:
        return 4

    # Contiguous run: the query appears inside the descriptor in order.
    n = len(query_tokens)
    for i in range(len(candidate_tokens) - n + 1):
        if candidate_tokens[i : i + n] == query_tokens:
            return 3

    hits = sum(1 for t in query_tokens if _token_hit(t, candidate_tokens))
    if hits == len(query_tokens):
        return 2

    coverage = hits / len(query_tokens)
    substantial = any(len(t) >= 4 for t in query_tokens)
    if coverage >= MIN_TOKEN_COVERAGE and substantial:
        return 1
    if _similar(" ".join(query_tokens), " ".join(candidate_tokens)) >= FUZZY_WHOLE:
        return 1
    return 0


def find_merchant_matches(transactions: list[Transaction], query: str) -> list[MerchantMatch]:
    """
    Every transaction whose merchant is the one the user named, grouped by shop.

    Only the strongest tier that matched anything is returned, so an exact
    name never comes back padded with fuzzy near-misses. Results are grouped
    by cleaned display name, which is what collapses forty statement lines
    for the same shop into one row the user can read.
    """
    query_tokens = _tokens(query or "")
    if not query_tokens:
        return []

    scored: list[tuple[int, Transaction]] = []
    for transaction in transactions:
        tier = _tier(query_tokens, transaction.merchant_name or "")
        if tier > 0:
            scored.append((tier, transaction))
    if not scored:
        return []

    # Two bands, not four. The tiers rank HOW the name matched, but exact,
    # contiguous-phrase and all-tokens-present all mean "the user named this
    # merchant" -- so they must be answered together. Ranking them against
    # each other made one question have two answers: "Smith's" totalled 26
    # transactions ($486.83) while "smiths food" totalled 5 ($163.24), because
    # the second phrasing reached an exact tier on a handful of descriptors
    # and discarded the rest of the same shop's rows.
    #
    # Fuzzy stays separate and below, so a real match is never padded with
    # guesses -- it is used only when nothing matched properly at all.
    STRONG = 2
    best_tier = max(tier for tier, _ in scored)
    cutoff = STRONG if best_tier >= STRONG else best_tier

    grouped: dict[str, MerchantMatch] = {}
    for tier, transaction in scored:
        if tier < cutoff:
            continue
        raw = transaction.merchant_name or ""
        display = clean_merchant_name(raw)
        # Grouped on the COMPARABLE form, not the printed one, so 'Smiths-Food'
        # and 'Smiths Food' are one shop rather than two rows differing by a
        # hyphen. The display name is chosen separately, below.
        match = grouped.setdefault(merchant_key(display), MerchantMatch(display_name=display))
        match.transactions.append(transaction)
        match.raw_names.add(raw)

    for match in grouped.values():
        match.display_name = _preferred_display(match.raw_names)
    return sorted(grouped.values(), key=lambda m: len(m.transactions), reverse=True)


def suggest_merchants(transactions: list[Transaction], query: str, limit: int = 5) -> list[str]:
    """
    The closest merchants the user actually has, when the query matched none.

    'I have no record of Glato -- did you mean Gelato Bar?' is a useful
    answer. '$0.00' is not, and the user cannot tell it apart from a real
    zero.
    """
    query_tokens = _tokens(query or "")
    if not query_tokens:
        return []
    needle = " ".join(query_tokens)

    best: dict[str, float] = {}
    for transaction in transactions:
        raw = transaction.merchant_name or ""
        candidate_tokens = _tokens(raw)
        if not candidate_tokens:
            continue
        score = _similar(needle, " ".join(candidate_tokens))
        for query_token in query_tokens:
            for token in candidate_tokens:
                score = max(score, _similar(query_token, token))
        display = clean_merchant_name(raw)
        if score > best.get(display, 0.0):
            best[display] = score

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    # Held deliberately high. A suggestion is a claim that this might be what
    # the user meant, and three unrelated shop names read as the assistant
    # guessing -- worse than saying plainly that nothing came close.
    return [name for name, score in ranked[:limit] if score >= SUGGEST_THRESHOLD]


def top_merchants(transactions: list[Transaction], limit: int = 8) -> list[dict]:
    """
    Where the money in this set of transactions actually went, biggest first.

    Grouped by cleaned name, so the list reads as shops rather than as
    statement lines -- and so every name in it is one the user can ask a
    follow-up question about and get a match.
    """
    from decimal import Decimal

    from app.utils.money import quantize
    from app.utils.transactions import expense_amount, is_expense

    totals: dict[str, dict] = {}
    for transaction in transactions:
        if not is_expense(transaction):
            continue
        display = clean_merchant_name(transaction.merchant_name)
        entry = totals.setdefault(
            merchant_key(display), {"names": set(), "total": Decimal("0"), "count": 0}
        )
        entry["names"].add(transaction.merchant_name or "")
        entry["total"] += expense_amount(transaction)
        entry["count"] += 1

    ranked = sorted(totals.values(), key=lambda e: e["total"], reverse=True)[:limit]
    return [
        {
            "merchant": _preferred_display(e["names"]),
            "total": str(quantize(e["total"])),
            "count": e["count"],
        }
        for e in ranked
    ]
