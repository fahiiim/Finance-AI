"""
Monetary arithmetic helpers.

Everything here uses Decimal. Floats never touch authoritative financial
calculations — see financial-ai-analytics-engineer skill.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.core.exceptions import ValidationFailedError

CENTS = Decimal("0.01")


def quantize(amount: Decimal) -> Decimal:
    """Round to 2 decimal places using standard half-up rounding for currency display."""
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def sum_amounts(amounts: list[Decimal]) -> Decimal:
    total = Decimal("0")
    for a in amounts:
        total += a
    return quantize(total)


def percentage_change(current: Decimal, previous: Decimal) -> Decimal | None:
    """
    Returns None (not zero, not an error) when previous is 0 — a percentage
    change against a zero baseline is mathematically undefined and must not
    be silently reported as 0% or infinity.
    """
    if previous == 0:
        return None
    change = ((current - previous) / previous) * Decimal("100")
    return change.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def require_same_currency(currencies: list[str]) -> str:
    """
    Raises if a set of amounts spans more than one currency. Financial totals
    must never silently mix currencies (see analytics skill: CURRENCY).
    """
    unique = set(currencies)
    if len(unique) > 1:
        raise ValidationFailedError(
            "Cannot combine amounts across multiple currencies without explicit conversion.",
            details={"currencies": sorted(unique)},
        )
    if not unique:
        return "USD"
    return unique.pop()
