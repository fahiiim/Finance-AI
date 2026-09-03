from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.schemas.financial import Transaction
from app.tools.analytics import compare_periods, detect_spending_spike


def _txn(amount: str, day: date) -> Transaction:
    return Transaction(transaction_id="t", account_id="a", amount=Decimal(amount), date=day)


def test_detect_spending_spike_flags_outlier_day():
    txns = [
        _txn("20", date(2026, 8, 1)),
        _txn("22", date(2026, 8, 2)),
        _txn("18", date(2026, 8, 3)),
        _txn("21", date(2026, 8, 4)),
        _txn("500", date(2026, 8, 5)),  # clear outlier
    ]
    analysis = detect_spending_spike(txns)
    assert len(analysis.spikes) == 1
    assert analysis.spikes[0].day == date(2026, 8, 5)


def test_detect_spending_spike_no_data_returns_empty():
    analysis = detect_spending_spike([])
    assert analysis.spikes == []
    assert analysis.mean_daily_spending == Decimal("0")


def test_detect_spending_spike_respects_min_amount_floor():
    # Small, evenly-varying spending shouldn't be flagged even if one day
    # deviates by a large stddev multiple, because it's below the floor.
    txns = [
        _txn("1", date(2026, 8, 1)),
        _txn("1", date(2026, 8, 2)),
        _txn("5", date(2026, 8, 3)),
    ]
    analysis = detect_spending_spike(txns, min_spike_amount=Decimal("50"))
    assert analysis.spikes == []


def test_compare_periods_computes_difference_and_percentage():
    current = [_txn("150", date(2026, 8, 1))]
    previous = [_txn("100", date(2026, 7, 1))]
    result = compare_periods(current, previous)
    assert result.current_total == Decimal("150")
    assert result.previous_total == Decimal("100")
    assert result.difference == Decimal("50")
    assert result.percentage_change == Decimal("50.0")


def test_compare_periods_zero_previous_gives_none_percentage():
    current = [_txn("150", date(2026, 8, 1))]
    result = compare_periods(current, [])
    assert result.percentage_change is None
