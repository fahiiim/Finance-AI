"""
Analytics tools built on top of the deterministic calculation engine.

SPENDING SPIKE METHODOLOGY (documented per spec section 30 — this is an
implementation assumption, not a client-validated methodology, since none
was provided; see docs/assumptions.md):

  1. Group transactions (outflows only) by calendar day within the analysis
     window.
  2. Compute the mean and population standard deviation of daily totals.
  3. Flag any day whose total exceeds `mean + spike_threshold_stddev *
     stddev` (default threshold: 1.5 standard deviations) AND exceeds an
     absolute floor of `min_spike_amount` (default: 50 units of the user's
     currency), to avoid flagging noise when spending is small and uniform.
  4. Days are returned sorted by deviation magnitude, most significant first.

This threshold is intentionally configurable (function parameters) rather
than hardcoded, and every value in the output is either a FACT (the day's
total) or a CALCULATION (mean/deviation) — never an LLM guess.
"""

from __future__ import annotations

import statistics
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.financial import Transaction
from app.utils.money import percentage_change, quantize
from app.utils.transactions import expense_amount, is_expense


class DailySpendingSpike(BaseModel):
    day: date
    total: Decimal
    deviation_from_mean: Decimal
    stddev_multiple: Decimal


class SpikeAnalysis(BaseModel):
    mean_daily_spending: Decimal
    stddev_daily_spending: Decimal
    spikes: list[DailySpendingSpike]


def detect_spending_spike(
    transactions: list[Transaction],
    *,
    spike_threshold_stddev: float = 1.5,
    min_spike_amount: Decimal = Decimal("50"),
) -> SpikeAnalysis:
    daily_totals: dict[date, Decimal] = {}
    for t in transactions:
        if not is_expense(t):
            continue
        daily_totals[t.date] = daily_totals.get(t.date, Decimal("0")) + expense_amount(t)

    if not daily_totals:
        return SpikeAnalysis(mean_daily_spending=Decimal("0"), stddev_daily_spending=Decimal("0"), spikes=[])

    values = [float(v) for v in daily_totals.values()]
    mean = statistics.fmean(values)
    stddev = statistics.pstdev(values) if len(values) > 1 else 0.0

    spikes: list[DailySpendingSpike] = []
    for day, total in daily_totals.items():
        deviation = float(total) - mean
        multiple = (deviation / stddev) if stddev > 0 else 0.0
        if multiple >= spike_threshold_stddev and total >= min_spike_amount:
            spikes.append(
                DailySpendingSpike(
                    day=day,
                    total=quantize(total),
                    deviation_from_mean=quantize(Decimal(str(deviation))),
                    stddev_multiple=Decimal(str(round(multiple, 2))),
                )
            )

    spikes.sort(key=lambda s: s.stddev_multiple, reverse=True)
    return SpikeAnalysis(
        mean_daily_spending=quantize(Decimal(str(mean))),
        stddev_daily_spending=quantize(Decimal(str(stddev))),
        spikes=spikes,
    )


class PeriodComparison(BaseModel):
    current_total: Decimal
    previous_total: Decimal
    difference: Decimal
    percentage_change: Decimal | None  # None = undefined (previous period was zero)


def compare_periods(current_transactions: list[Transaction], previous_transactions: list[Transaction]) -> PeriodComparison:
    current_total = sum((expense_amount(t) for t in current_transactions if is_expense(t)), Decimal("0"))
    previous_total = sum((expense_amount(t) for t in previous_transactions if is_expense(t)), Decimal("0"))
    return PeriodComparison(
        current_total=quantize(current_total),
        previous_total=quantize(previous_total),
        difference=quantize(current_total - previous_total),
        percentage_change=percentage_change(current_total, previous_total),
    )
