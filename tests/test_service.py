"""Tests for the CurrencyService — rate lookups, conversions, edge cases."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.services import CurrencyService, SENTINEL_VALUES


def _make_service(tmp_path: Path, rates: dict[str, dict[str, str]]) -> CurrencyService:
    """Build a CurrencyService backed by ``tmp_path`` with the given
    per-currency rate dicts. Static reference data still comes from
    the bundled JSONs under src/data/."""
    rates_subdir = tmp_path / "rates"
    rates_subdir.mkdir()
    for ccy, daily in rates.items():
        (rates_subdir / f"{ccy}.json").write_text(json.dumps(daily))
    return CurrencyService.load(tmp_path)


def test_eur_to_eur_is_identity(tmp_path):
    svc = _make_service(tmp_path, {"USD": {"2024-01-02": "1.10"}})
    res = svc.convert_detailed(Decimal("100"), "EUR", date(2024, 1, 2))
    assert res.eur == Decimal("100.00")
    assert res.source == "identity"


def test_usd_to_eur_uses_loaded_rate(tmp_path):
    svc = _make_service(tmp_path, {"USD": {"2024-01-02": "1.10"}})
    res = svc.convert_detailed(Decimal("110"), "USD", date(2024, 1, 2))
    assert res.eur == Decimal("100.00")
    assert res.source == "ecb"


def test_lookback_walks_to_friday_for_weekend(tmp_path):
    """Sunday 2024-01-07 should reuse Friday 2024-01-05's rate."""
    svc = _make_service(tmp_path, {"USD": {"2024-01-05": "1.10"}})
    res = svc.convert_detailed(Decimal("110"), "USD", date(2024, 1, 7))
    assert res.eur == Decimal("100.00")
    assert res.rate_date == date(2024, 1, 5)


def test_sentinel_values_parse_as_undisclosed():
    for s in SENTINEL_VALUES:
        value, was_sentinel = CurrencyService.parse_value(s)
        assert value is None
        assert was_sentinel is True


def test_normal_value_parses_through():
    value, was_sentinel = CurrencyService.parse_value("1234.56")
    assert value == Decimal("1234.56")
    assert was_sentinel is False


def test_unknown_currency_returns_unknown(tmp_path):
    svc = _make_service(tmp_path, {"USD": {"2024-01-02": "1.10"}})
    res = svc.convert_detailed(Decimal("100"), "XXX", date(2024, 1, 2))
    assert res.eur is None
    assert res.source == "unknown"


def test_locked_eek_uses_locked_rate_post_2011(tmp_path):
    """Estonia switched to EUR on 2011-01-01 at 1 EUR = 15.6466 EEK.
    After that date we must use the locked rate, not whatever the
    daily file has (the daily file shouldn't have post-2011 data,
    but the locked-rates table takes priority either way)."""
    svc = _make_service(tmp_path, {})
    res = svc.convert_detailed(Decimal("15.6466"), "EEK", date(2012, 5, 1))
    assert res.eur == Decimal("1.00")
    assert res.source == "locked"


def test_currency_for_country_resolves_on_history(tmp_path):
    svc = _make_service(tmp_path, {})
    assert svc.currency_for("DEU", date(2024, 1, 1)) == "EUR"
    assert svc.currency_for("USA", date(2024, 1, 1)) == "USD"


def test_resolve_currency_inference_from_country(tmp_path):
    svc = _make_service(tmp_path, {})
    ccy, inferred = svc.resolve_currency(None, "FRA", date(2024, 1, 1))
    assert ccy == "EUR"
    assert inferred is True


@pytest.mark.parametrize("alias,canonical", [
    ("USN", "USD"),  # IMF unit-of-account alias → USD per aliases.json
])
def test_alias_resolution(alias, canonical, tmp_path):
    svc = _make_service(tmp_path, {})
    assert svc.normalize_currency(alias) == canonical
