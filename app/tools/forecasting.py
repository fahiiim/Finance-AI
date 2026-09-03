"""
Spending forecasts.

Forecasts are ESTIMATES, never facts. The output schema carries an explicit
`is_estimate=True` marker and a plain-language `methodology_note` so the
response-generation prompt can never present a forecast as guaranteed (see
financial-buddy-prompt-engineer skill).

METHODOLOGY (documented assumption — see docs/assumptions.md): simple
average daily spending over the trailing lookback window, projected forward
linearly. This is intentionally simple and explainable; a more
sophisticated model (seasonality, trend detection) can replace this
function without touching any caller, since callers only depend on the
ForecastResult shape.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.financial import Transaction
from app.tools.calculations import calculate_average_daily_spending
from app.utils.money import quantize


class ForecastResult(BaseModel):
    is_estimate: bool = True
    projected_total: Decimal
    forecast_days: int
    based_on_average_daily_spending: Decimal
    lookback_days: int
    methodology_note: str = (
        "Estimated by projecting the average daily spending over the trailing "
        "lookback window forward — not a guarantee of future spending."
    )


def forecast_spending(
    lookback_transactions: list[Transaction],
    *,
    lookback_start: date,
    lookback_end: date,
    forecast_days: int,
) -> ForecastResult | None:
    avg_daily = calculate_average_daily_spending(lookback_transactions, start=lookback_start, end=lookback_end)
    if avg_daily is None:
        return None
    lookback_days = (lookback_end - lookback_start).days + 1
    projected = quantize(avg_daily * Decimal(forecast_days))
    return ForecastResult(
        projected_total=projected,
        forecast_days=forecast_days,
        based_on_average_daily_spending=avg_daily,
        lookback_days=lookback_days,
    )
