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


# ── Prices quoted in a minor unit (pence, cents, agorot) ─────────────
# Yahoo labels London prices GBp and Johannesburg ZAc; LSE's own lists
# say GBX. Upper-casing GBp read it as GBP, so a 1,234p share price
# became £1,234: a hundred times too much.

ON = date(2024, 1, 2)
MINOR_RATES = {
    "GBP": {"2024-01-02": "0.85"},
    "ZAR": {"2024-01-02": "20.00"},
    "ILS": {"2024-01-02": "4.00"},
}


@pytest.mark.parametrize("code", ["GBp", "GBX", "gbx"])
def test_pence_convert_as_pence(code, tmp_path):
    svc = _make_service(tmp_path, MINOR_RATES)
    res = svc.convert_detailed(Decimal("1234"), code, ON)
    assert res.eur == Decimal("14.52")  # £12.34 at 0.85
    assert (res.currency, res.scale) == ("GBP", Decimal("0.01"))
    assert res.rate_used == Decimal("0.85")


@pytest.mark.parametrize("code", ["GBP", "gbp"])
def test_pounds_stay_pounds(code, tmp_path):
    svc = _make_service(tmp_path, MINOR_RATES)
    res = svc.convert_detailed(Decimal("1234"), code, ON)
    assert res.eur == Decimal("1451.76")
    assert (res.currency, res.scale) == ("GBP", Decimal("1"))


@pytest.mark.parametrize("code,value", [("ZAc", "5000"), ("ILA", "1000")])
def test_other_minor_units_convert_through_their_currency(code, value, tmp_path):
    svc = _make_service(tmp_path, MINOR_RATES)
    assert svc.to_eur(Decimal(value), code, ON) == Decimal("2.50")


def test_minor_unit_in_a_currency_without_a_rate_is_unknown(tmp_path):
    svc = _make_service(tmp_path, {"GBP": {"2024-01-02": "0.85"}})
    res = svc.convert_detailed(Decimal("5000"), "ZAc", ON)
    assert res.eur is None
    assert res.source == "unknown"


def test_every_minor_unit_code_names_an_iso_currency_with_minor_units(tmp_path):
    """The factor comes from the ISO 4217 list, so a code naming a
    currency the list doesn't divide (or doesn't know) has no factor."""
    svc = _make_service(tmp_path, {})
    codes = json.loads((Path(__file__).resolve().parent.parent / "src" / "data"
                        / "minor_unit_codes.json").read_text(encoding="utf-8"))
    for code, currency in codes.items():
        if code.startswith("_"):
            continue
        assert svc.unit(code)[0] == currency, code
        assert Decimal(0) < svc.unit(code)[1] < Decimal(1), code
