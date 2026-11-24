"""Tests for the FastAPI surface."""
import json
from datetime import date as _date, timedelta as _timedelta
from importlib import reload
from pathlib import Path

from fastapi.testclient import TestClient


def _seed_pvc(tmp_path: Path, more: dict[str, dict[str, str]] | None = None) -> Path:
    rates = tmp_path / "rates"
    rates.mkdir()
    (rates / "USD.json").write_text(json.dumps(
        {"2000-01-03": "1.00", "2024-01-02": "1.10", _date.today().isoformat(): "1.10"}))
    for ccy, daily in (more or {}).items():
        (rates / f"{ccy}.json").write_text(json.dumps(daily))
    return tmp_path


def _client(monkeypatch, tmp_path, more=None):
    pvc = _seed_pvc(tmp_path, more)
    monkeypatch.setenv("CURRENCY_DATA_DIR", str(pvc))
    monkeypatch.setenv("RELOAD_TOKEN", "test-token")
    # Re-import so the env vars stick + the _holder is fresh.
    import src.api.app as app_mod  # pylint: disable=import-outside-toplevel
    reload(app_mod)
    return TestClient(app_mod.app), app_mod


def test_healthz_is_always_ok(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.get("/healthz").json() == {"ok": True}


def test_readyz_503_until_pvc_populated(monkeypatch, tmp_path):
    monkeypatch.setenv("CURRENCY_DATA_DIR", str(tmp_path))  # no rates/
    import src.api.app as app_mod  # pylint: disable=import-outside-toplevel
    reload(app_mod)
    client = TestClient(app_mod.app)
    assert client.get("/readyz").status_code == 503


def test_readyz_200_once_pvc_has_rates(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["currencies"] >= 1
    assert r.json()["history_since_2000"] is True


def test_readyz_503_when_rates_stale(monkeypatch, tmp_path):
    rates = tmp_path / "rates"
    rates.mkdir()
    old = (_date.today() - _timedelta(days=30)).isoformat()
    (rates / "USD.json").write_text(json.dumps({"2000-01-03": "1.0", old: "1.1"}))
    monkeypatch.setenv("CURRENCY_DATA_DIR", str(tmp_path))
    import src.api.app as app_mod  # pylint: disable=import-outside-toplevel
    reload(app_mod)
    r = TestClient(app_mod.app).get("/readyz")
    assert r.status_code == 503
    assert "stale" in r.json()["detail"]


def test_parse_sentinel(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/v1/parse", json={"raw": "-1"})
    assert r.json() == {"value": None, "was_sentinel": True}


def test_parse_normal_value(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/v1/parse", json={"raw": "1234.56"})
    assert r.json() == {"value": "1234.56", "was_sentinel": False}


def test_resolve_uses_country_default(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/v1/resolve", json={
        "declared": None, "country": "DEU", "on": "2024-01-15",
    })
    assert r.json() == {"currency": "EUR", "inferred": True, "scale": "1"}


def test_convert_usd_to_eur(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/v1/convert", json={
        "value": "110", "currency": "USD", "on": "2024-01-02",
    })
    body = r.json()
    assert body["eur"] == "100.00"
    assert body["source"] == "ecb"


def test_convert_eur_identity(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/v1/convert", json={
        "value": "100", "currency": "EUR", "on": "2024-01-02",
    })
    body = r.json()
    assert body["eur"] == "100.00"
    assert body["source"] == "identity"


def test_reload_requires_token(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.post("/v1/reload").status_code == 401
    assert client.post(
        "/v1/reload", headers={"X-Reload-Token": "wrong"},
    ).status_code == 401


def test_reload_succeeds_with_token(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/v1/reload", headers={"X-Reload-Token": "test-token"})
    assert r.json() == {"ok": True}


def _rewrite_usd(pvc: Path, rate: str) -> None:
    """What a loader run leaves behind: new rate files, then metadata.json."""
    (pvc / "rates" / "USD.json").write_text(json.dumps(
        {"2000-01-03": "1.00", "2024-01-02": rate, _date.today().isoformat(): rate}))
    (pvc / "metadata.json").write_text(json.dumps({"last_refreshed": "now"}))


def test_new_rates_on_disk_are_served_without_a_reload_call(monkeypatch, tmp_path):
    monkeypatch.setenv("RELOAD_CHECK_SECONDS", "0")
    client, _ = _client(monkeypatch, tmp_path)
    ask = {"value": "110", "currency": "USD", "on": "2024-01-02"}
    assert client.post("/v1/convert", json=ask).json()["eur"] == "100.00"

    _rewrite_usd(tmp_path, "2.20")

    assert client.post("/v1/convert", json=ask).json()["eur"] == "50.00"


def test_rates_on_disk_are_rechecked_only_once_per_interval(monkeypatch, tmp_path):
    monkeypatch.setenv("RELOAD_CHECK_SECONDS", "3600")
    client, _ = _client(monkeypatch, tmp_path)
    ask = {"value": "110", "currency": "USD", "on": "2024-01-02"}
    assert client.post("/v1/convert", json=ask).json()["eur"] == "100.00"

    _rewrite_usd(tmp_path, "2.20")

    assert client.post("/v1/convert", json=ask).json()["eur"] == "100.00"


GBP = {"GBP": {"2024-01-02": "0.85"}}


def test_convert_reads_pence_as_pence(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, GBP)
    r = client.post("/v1/convert", json={
        "value": "1234", "currency": "GBp", "on": "2024-01-02",
    })
    body = r.json()
    assert body["eur"] == "14.52"
    assert (body["currency"], body["scale"]) == ("GBP", "0.01")


def test_resolve_names_the_iso_currency_and_its_scale(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, GBP)
    for declared, scale in (("GBp", "0.01"), ("GBP", "1"), ("ZAc", "0.01")):
        r = client.post("/v1/resolve", json={"declared": declared})
        assert r.json()["scale"] == scale, declared
    assert client.post("/v1/resolve", json={"declared": "GBp"}).json()["currency"] == "GBP"


def test_rates_give_each_day_what_convert_would_use(monkeypatch, tmp_path):
    """Friday's rate carries over the weekend, as /v1/convert's does."""
    client, _ = _client(monkeypatch, tmp_path, {"GBP": {"2024-01-05": "0.86",
                                                        "2024-01-08": "0.87"}})
    body = client.get("/v1/rates", params={
        "currency": "GBp", "start": "2024-01-05", "end": "2024-01-08"}).json()
    assert (body["currency"], body["scale"]) == ("GBP", "0.01")
    assert body["rates"] == {"2024-01-05": "0.86", "2024-01-06": "0.86",
                             "2024-01-07": "0.86", "2024-01-08": "0.87"}
    convert = client.post("/v1/convert", json={
        "value": "100", "currency": "GBP", "on": "2024-01-07"}).json()
    assert convert["rate_used"] == body["rates"]["2024-01-07"]


def test_rates_for_euros_are_one_and_unknown_codes_have_none(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    eur = client.get("/v1/rates", params={
        "currency": "EUR", "start": "2024-01-01", "end": "2024-01-02"}).json()
    assert eur["rates"] == {"2024-01-01": "1", "2024-01-02": "1"}
    unknown = client.get("/v1/rates", params={
        "currency": "XYZ", "start": "2024-01-01", "end": "2024-01-02"}).json()
    assert unknown["rates"] == {}


def test_rates_refuse_a_backwards_or_huge_range(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.get("/v1/rates", params={
        "currency": "USD", "start": "2024-01-02", "end": "2024-01-01"}).status_code == 422
    assert client.get("/v1/rates", params={
        "currency": "USD", "start": "1990-01-01", "end": "2024-01-01"}).status_code == 422
