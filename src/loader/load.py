"""
Exchange-rate loader
====================
Downloads daily exchange rates from the ECB Statistical Data
Warehouse (https://data-api.ecb.europa.eu/) and from Frankfurter
(https://api.frankfurter.app/) for currencies ECB doesn't cover,
and writes one ``rates/{CCY}.json`` file per currency under the
service's PVC.

The service's API reads those JSON files at request time (or once
at boot, depending on the deployment posture). Consumers call the
service over HTTP; nothing in this repo touches the central event
log — that decoupling is the whole point of running this as a
standalone service.

Usage:
    python -m src.loader.load
    python -m src.loader.load --rates-dir /tmp/currency --start 2024-01-01
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Currencies the ECB publishes daily reference rates for
ECB_CURRENCIES = [
    "USD", "JPY", "BGN", "CZK", "DKK", "GBP", "HUF", "PLN", "RON", "SEK",
    "CHF", "ISK", "NOK", "TRY", "AUD", "BRL", "CAD", "CNY", "HKD", "IDR",
    "ILS", "INR", "KRW", "MXN", "MYR", "NZD", "PHP", "SGD", "THB", "ZAR",
    # Historical (locked currencies — ECB still has the rates pre-lock)
    "HRK",
]

# Currencies needed by TED / fontem data but NOT in ECB. Frankfurter
# (free, no auth, daily rates back to 2000) covers them.
NON_ECB_CURRENCIES = [
    "MDL", "MKD", "UAH", "RSD", "BAM", "MAD", "TND", "AMD",
    "AWG", "GEL", "ALL", "DZD", "EGP", "ARS", "RUB",
]

ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
    "?startPeriod={start}&endPeriod={end}&format=csvdata"
)
FRANKFURTER_URL = "https://api.frankfurter.app/{start}..{end}?from=EUR&to={ccy}"

CONTACT_EMAIL = "team@fontem.eu"
HTTP_HEADERS = {
    "User-Agent": f"Fontem-Currency/1.0 (+https://fontem.eu; {CONTACT_EMAIL})",
    "From": CONTACT_EMAIL,
    "Accept": "*/*",
}


def _local_rates_path(rates_dir: Path, ccy: str) -> Path:
    return rates_dir / "rates" / f"{ccy}.json"


def _if_modified_since(path: Path) -> str | None:
    """Return an RFC-1123 If-Modified-Since value pinned to the
    local rates file's mtime, or None when no cached file exists."""
    try:
        ts = path.stat().st_mtime
    except FileNotFoundError:
        return None
    return format_datetime(
        datetime.fromtimestamp(ts, tz=timezone.utc), usegmt=True,
    )


def fetch_ecb(
    ccy: str, start: str, end: str, rates_dir: Path | None = None,
) -> dict[str, str] | None:
    """Fetch ECB daily reference rates for one currency.

    Returns:
        dict {date: rate_str} when ECB returns data
        None when ECB returns 304 (caller keeps cached file, no rewrite)
        {} on fetch failure (caller can fall back to Frankfurter)
    """
    url = ECB_URL.format(ccy=ccy, start=start, end=end)
    headers = dict(HTTP_HEADERS)
    if rates_dir is not None:
        ims = _if_modified_since(_local_rates_path(rates_dir, ccy))
        if ims:
            headers["If-Modified-Since"] = ims
    try:
        resp = httpx.get(url, timeout=60, follow_redirects=True, headers=headers)
        if resp.status_code == 304:
            logger.info("  %s: ECB returned 304 (unchanged since %s)",
                        ccy, headers.get("If-Modified-Since"))
            return None
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("ECB fetch failed for %s: %s", ccy, exc)
        return {}

    daily: dict[str, str] = {}
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        period = row.get("TIME_PERIOD", "")
        obs = row.get("OBS_VALUE", "")
        if period and obs:
            try:
                float(obs)
                daily[period] = obs
            except ValueError:
                pass
    return daily


def fetch_frankfurter(ccy: str, start: str, end: str) -> dict[str, str]:
    """Fetch rates via Frankfurter API. Chunks by year — Frankfurter
    rejects ranges spanning more than a single calendar year."""
    daily: dict[str, str] = {}
    start_year = int(start[:4])
    end_year = int(end[:4])
    for year in range(start_year, end_year + 1):
        chunk_start = f"{year}-01-01" if year > start_year else start
        chunk_end = f"{year}-12-31" if year < end_year else end
        url = FRANKFURTER_URL.format(start=chunk_start, end=chunk_end, ccy=ccy)
        try:
            resp = httpx.get(url, timeout=60, follow_redirects=True,
                             headers=HTTP_HEADERS)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for d, rates in data.get("rates", {}).items():
                if ccy in rates:
                    daily[d] = str(rates[ccy])
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            logger.warning("Frankfurter fetch failed for %s %s: %s",
                           ccy, year, exc)
    return daily


def save_currency_file(rates_dir: Path, ccy: str, daily: dict[str, str]) -> None:
    """Write a per-currency JSON file."""
    rates_subdir = rates_dir / "rates"
    rates_subdir.mkdir(parents=True, exist_ok=True)
    out = rates_subdir / f"{ccy}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(daily, f, separators=(",", ":"), sort_keys=True)
    logger.info("  %s: %d rates saved (%s to %s)",
                ccy, len(daily),
                min(daily) if daily else "—",
                max(daily) if daily else "—")


def load_all(
    rates_dir: str | Path,
    start: str = "2000-01-01",
    end: str | None = None,
    currencies: list[str] | None = None,
) -> dict:
    """Refresh every currency: ECB primary, Frankfurter fallback."""
    rates_dir = Path(rates_dir)
    if end is None:
        end = date.today().isoformat()

    if currencies is None:
        ecb_list = list(ECB_CURRENCIES)
        non_ecb_list = list(NON_ECB_CURRENCIES)
    else:
        ecb_list = [c for c in currencies if c in ECB_CURRENCIES]
        non_ecb_list = [c for c in currencies if c not in ECB_CURRENCIES]

    logger.info("Loading %d ECB + %d non-ECB currencies (%s to %s)",
                len(ecb_list), len(non_ecb_list), start, end)

    summary = {"ccy_loaded": 0, "ccy_unchanged": 0, "ccy_failed": 0}

    for ccy in ecb_list:
        logger.info("Fetching %s from ECB...", ccy)
        daily = fetch_ecb(ccy, start, end, rates_dir=rates_dir)
        if daily is None:
            summary["ccy_unchanged"] += 1
            continue
        if not daily:
            logger.warning("  %s: ECB returned no data, trying Frankfurter", ccy)
            daily = fetch_frankfurter(ccy, start, end)
        if daily:
            save_currency_file(rates_dir, ccy, daily)
            summary["ccy_loaded"] += 1
        else:
            summary["ccy_failed"] += 1

    for ccy in non_ecb_list:
        logger.info("Fetching %s from Frankfurter...", ccy)
        daily = fetch_frankfurter(ccy, start, end)
        if daily:
            save_currency_file(rates_dir, ccy, daily)
            summary["ccy_loaded"] += 1
        else:
            summary["ccy_failed"] += 1

    metadata = {
        "last_refreshed": date.today().isoformat(),
        "start_period": start,
        "end_period": end,
        "ecb_currencies": ecb_list,
        "non_ecb_currencies": non_ecb_list,
        "sources": ["ecb", "frankfurter"],
    }
    with open(rates_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    return summary


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Refresh exchange rates")
    parser.add_argument(
        "--rates-dir",
        default=os.environ.get("CURRENCY_DATA_DIR", "/srv/currency-data"),
    )
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--currencies", nargs="+", default=None,
        help="Restrict to a specific list (default: all known)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    summary = load_all(
        args.rates_dir, args.start, args.end, args.currencies,
    )
    logger.info(
        "Done: %d loaded, %d unchanged, %d failed",
        summary["ccy_loaded"], summary["ccy_unchanged"], summary["ccy_failed"],
    )


if __name__ == "__main__":
    main()
