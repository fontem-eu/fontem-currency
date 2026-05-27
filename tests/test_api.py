"""Tests for the FastAPI surface."""
import json
from pathlib import Path

from fastapi.testclient import TestClient


def _seed_pvc(tmp_path: Path) -> Path:
    rates = tmp_path / "rates"
    rates.mkdir()
    (rates / "USD.json").write_text(json.dumps({"2024-01-02": "1.10"}))
    return tmp_path


def _client(monkeypatch, tmp_path):
    pvc = _seed_pvc(tmp_path)
    monkeypatch.setenv("CURRENCY_DATA_DIR", str(pvc))
    monkeypatch.setenv("RELOAD_TOKEN", "test-token")
    # Re-import so the env vars stick + the _holder is fresh.
    from importlib import reload  # pylint: disable=import-outside-toplevel
    import src.api.app as app_mod  # pylint: disable=import-outside-toplevel
    reload(app_mod)
    return TestClient(app_mod.app), app_mod


def test_healthz_is_always_ok(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.get("/healthz").json() == {"ok": True}


def test_readyz_503_until_pvc_populated(monkeypatch, tmp_path):
    monkeypatch.setenv("CURRENCY_DATA_DIR", str(tmp_path))  # no rates/
    from importlib import reload  # pylint: disable=import-outside-toplevel
    import src.api.app as app_mod  # pylint: disable=import-outside-toplevel
    reload(app_mod)
    client = TestClient(app_mod.app)
    assert client.get("/readyz").status_code == 503


def test_readyz_200_once_pvc_has_rates(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["currencies"] >= 1


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
    assert r.json() == {"currency": "EUR", "inferred": True}


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
