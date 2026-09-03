from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.exceptions import ValidationFailedError
from app.utils.money import percentage_change, require_same_currency, sum_amounts


def test_sum_amounts_rounds_to_cents():
    assert sum_amounts([Decimal("1.005"), Decimal("1.005")]) == Decimal("2.01")


def test_percentage_change_negative():
    assert percentage_change(Decimal("80"), Decimal("100")) == Decimal("-20.0")


def test_require_same_currency_passes_for_single_currency():
    assert require_same_currency(["USD", "USD", "USD"]) == "USD"


def test_require_same_currency_raises_on_mixed_currency():
    with pytest.raises(ValidationFailedError):
        require_same_currency(["USD", "EUR"])


def test_require_same_currency_defaults_usd_on_empty():
    assert require_same_currency([]) == "USD"
