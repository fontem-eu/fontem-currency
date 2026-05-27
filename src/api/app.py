"""
fontem-currency HTTP API
========================
FastAPI surface in front of the in-memory CurrencyService. Three
endpoints cover everything consumers (TED loader, future stats
ETL, the API server) need:

* ``POST /v1/parse``       — sentinel detection ("is -1 or 0.01 a
                              real value?")
* ``POST /v1/resolve``      — declared currency → effective currency
                              (handles aliases, country defaults,
                              currency-history-on-date)
* ``POST /v1/convert``      — value + currency + date → EUR

Plus operational endpoints:

* ``GET  /healthz``         — liveness; OK once the service has
                              loaded at least the static reference
                              files (no daily rates needed)
* ``GET  /readyz``          — readiness; OK once the daily rates
                              directory has been populated by the
                              loader cronjob at least once
* ``GET  /v1/coverage``      — per-currency earliest+latest date
                              we have a rate for (lets consumers
                              flag stale data)

The CurrencyService instance is constructed once at startup from
the PVC the loader writes into. Re-load is a SIGHUP-like operation
that the loader cronjob can trigger via ``POST /v1/reload`` after
it finishes a refresh (token-gated; see the deployment manifest).
"""
from __future__ import annotations

import logging
import os
from datetime import date as date_t
from decimal import Decimal
from pathlib import Path
from threading import RLock

from fastapi import FastAPI, HTTPException, Header, status
from pydantic import BaseModel, Field

from src.services import CurrencyService

logger = logging.getLogger(__name__)

RATES_DIR = Path(os.environ.get("CURRENCY_DATA_DIR", "/srv/currency-data"))
RELOAD_TOKEN = os.environ.get("RELOAD_TOKEN", "")


# ── In-memory service state ───────────────────────────────────────


class _Holder:
    """Wraps the singleton CurrencyService instance with a lock so
    ``POST /v1/reload`` can replace the instance atomically while
    the API keeps serving the previous one mid-request."""

    def __init__(self) -> None:
        self._svc: CurrencyService | None = None
        self._lock = RLock()

    def get(self) -> CurrencyService:
        with self._lock:
            if self._svc is None:
                self._svc = CurrencyService.load(RATES_DIR)
            return self._svc

    def reload(self) -> None:
        with self._lock:
            self._svc = CurrencyService.load(RATES_DIR)


_holder = _Holder()


# ── Pydantic request / response shapes ────────────────────────────


class ParseRequest(BaseModel):
    raw: str | float | int | None


class ParseResponse(BaseModel):
    value: str | None = Field(
        None,
        description=(
            "Parsed value as a decimal string (preserves precision). "
            "None when the raw value parses to one of the TED sentinel "
            "markers."
        ),
    )
    was_sentinel: bool


class ResolveRequest(BaseModel):
    declared: str | None = None
    country: str | None = Field(
        None, description="ISO 3166-1 alpha-3 (PRT, DEU, ...)",
    )
    on: date_t | None = None


class ResolveResponse(BaseModel):
    currency: str | None
    inferred: bool


class ConvertRequest(BaseModel):
    value: str | float | int | None
    currency: str | None
    on: date_t | None


class ConvertResponse(BaseModel):
    eur: str | None = Field(
        None, description="EUR amount as decimal string (preserves precision)",
    )
    rate_used: str | None = None
    rate_date: date_t | None = None
    source: str


class CoverageEntry(BaseModel):
    currency: str
    earliest: date_t | None
    latest: date_t | None


class CoverageResponse(BaseModel):
    coverage: list[CoverageEntry]


# ── App + endpoints ───────────────────────────────────────────────


app = FastAPI(title="fontem-currency", version="1")


@app.get("/healthz")
def healthz() -> dict:
    # The healthz must succeed even with no daily rates loaded —
    # static reference data alone is enough for /resolve to work
    # for many countries that use EUR or a locked currency.
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict:
    # Readyz waits for the loader to have populated at least one
    # daily-rate file. Without that, /convert calls would fall
    # straight through to "unknown" for every non-EUR currency.
    rates_subdir = RATES_DIR / "rates"
    if not rates_subdir.exists() or not any(rates_subdir.glob("*.json")):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no daily rates loaded yet (cronjob hasn't run?)",
        )
    return {"ok": True, "currencies": len(list(rates_subdir.glob("*.json")))}


@app.post("/v1/parse", response_model=ParseResponse)
def parse(req: ParseRequest) -> ParseResponse:
    value, was_sentinel = CurrencyService.parse_value(req.raw)
    return ParseResponse(
        value=None if value is None else str(value),
        was_sentinel=was_sentinel,
    )


@app.post("/v1/resolve", response_model=ResolveResponse)
def resolve(req: ResolveRequest) -> ResolveResponse:
    svc = _holder.get()
    ccy, inferred = svc.resolve_currency(req.declared, req.country, req.on)
    return ResolveResponse(currency=ccy, inferred=inferred)


@app.post("/v1/convert", response_model=ConvertResponse)
def convert(req: ConvertRequest) -> ConvertResponse:
    svc = _holder.get()
    value: Decimal | None = None
    if req.value is not None:
        try:
            value = Decimal(str(req.value))
        except (ValueError, ArithmeticError):
            value = None
    result = svc.convert_detailed(value, req.currency, req.on)
    return ConvertResponse(
        eur=None if result.eur is None else str(result.eur),
        rate_used=None if result.rate_used is None else str(result.rate_used),
        rate_date=result.rate_date,
        source=result.source,
    )


@app.get("/v1/coverage", response_model=CoverageResponse)
def coverage() -> CoverageResponse:
    svc = _holder.get()
    out = []
    for ccy in svc.known_currencies():
        earliest, latest = svc.currency_coverage(ccy)
        out.append(
            CoverageEntry(currency=ccy, earliest=earliest, latest=latest),
        )
    return CoverageResponse(coverage=out)


@app.post("/v1/reload")
def reload_rates(
    x_reload_token: str = Header(default=""),
) -> dict:
    """Trigger an in-process reload of the rates directory.

    Token-gated so an exposed pod (we run it cluster-internal-only
    today, but defence-in-depth) can't have its in-memory state
    flapped by an attacker. The loader cronjob hits this once the
    refresh finishes; the deployment env var ``RELOAD_TOKEN``
    matches a Vault secret the cronjob reads from the same secret.
    """
    if not RELOAD_TOKEN or x_reload_token != RELOAD_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid reload token",
        )
    _holder.reload()
    return {"ok": True}
