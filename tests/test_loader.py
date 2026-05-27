"""Tests for the exchange-rate loader (offline — no network calls)."""
# pylint: disable=protected-access
import json
from unittest.mock import MagicMock, patch

from src.loader import load as loader_mod


def test_save_currency_file_creates_subdir_and_writes_json(tmp_path):
    loader_mod.save_currency_file(tmp_path, "USD", {"2024-01-02": "1.10"})
    out = tmp_path / "rates" / "USD.json"
    assert out.is_file()
    assert json.loads(out.read_text()) == {"2024-01-02": "1.10"}


def test_save_uses_compact_separators_and_sorted_keys(tmp_path):
    loader_mod.save_currency_file(tmp_path, "X", {"2024-01-02": "2", "2024-01-01": "1"})
    body = (tmp_path / "rates" / "X.json").read_text()
    assert body == '{"2024-01-01":"1","2024-01-02":"2"}'


def test_if_modified_since_returns_rfc1123_when_file_exists(tmp_path):
    rates = tmp_path / "rates"
    rates.mkdir()
    f = rates / "USD.json"
    f.write_text("{}")
    ims = loader_mod._if_modified_since(f)
    assert ims is not None
    # RFC 1123 ends with "GMT"
    assert ims.endswith("GMT")


def test_if_modified_since_returns_none_for_missing_file(tmp_path):
    assert loader_mod._if_modified_since(tmp_path / "nope.json") is None


def test_fetch_ecb_returns_none_on_304(tmp_path, monkeypatch):
    """ECB 304 means "unchanged since IMS" — the loader must propagate
    None so the caller skips the save/emit cycle for that currency."""
    fake_resp = MagicMock()
    fake_resp.status_code = 304
    monkeypatch.setattr(loader_mod.httpx, "get", lambda *a, **kw: fake_resp)
    # Need an existing cached file so the IMS header gets sent.
    (tmp_path / "rates").mkdir()
    (tmp_path / "rates" / "USD.json").write_text("{}")
    out = loader_mod.fetch_ecb("USD", "2024-01-01", "2024-01-31",
                               rates_dir=tmp_path)
    assert out is None


def test_fetch_ecb_parses_csv_rows():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = (
        "KEY,TIME_PERIOD,OBS_VALUE\n"
        "EXR.D.USD.EUR.SP00.A,2024-01-02,1.0956\n"
        "EXR.D.USD.EUR.SP00.A,2024-01-03,1.0921\n"
    )
    fake_resp.raise_for_status = MagicMock()
    with patch.object(loader_mod.httpx, "get", return_value=fake_resp):
        out = loader_mod.fetch_ecb("USD", "2024-01-01", "2024-01-31")
    assert out == {"2024-01-02": "1.0956", "2024-01-03": "1.0921"}


def test_fetch_ecb_returns_empty_on_transport_error():
    with patch.object(loader_mod.httpx, "get",
                      side_effect=loader_mod.httpx.ConnectTimeout("boom")):
        out = loader_mod.fetch_ecb("USD", "2024-01-01", "2024-01-31")
    assert out == {}


def test_load_all_writes_metadata_with_period(tmp_path, monkeypatch):
    """End-to-end with everything mocked: load_all should still write
    metadata.json reflecting the requested period."""
    monkeypatch.setattr(loader_mod, "fetch_ecb",
                        lambda *a, **kw: {"2024-01-02": "1.10"})
    monkeypatch.setattr(loader_mod, "fetch_frankfurter",
                        lambda *a, **kw: {})
    summary = loader_mod.load_all(
        tmp_path, start="2024-01-01", end="2024-01-31",
        currencies=["USD"],
    )
    assert summary["ccy_loaded"] == 1
    meta = json.loads((tmp_path / "metadata.json").read_text())
    assert meta["start_period"] == "2024-01-01"
    assert meta["end_period"] == "2024-01-31"
    assert meta["ecb_currencies"] == ["USD"]
