# fontem-currency

Currency reference + EUR conversion service. **Singleton — one deployment in the `currency-service` namespace serves every environment** (testing / staging / dast / prod) over cluster DNS. Mirrors the `fontem-linguistics` posture: non-critical, offline data sources, cluster-internal only.

## What it does

* Daily exchange rates from the ECB Statistical Data Warehouse (primary, ~30 currencies).
* Frankfurter as the fallback for currencies ECB doesn't carry (MDL, MKD, UAH, RSD, BAM, …).
* In-process `CurrencyService` with sentinel detection (TED's `-1` / `0.01` are "value undisclosed"), currency-history lookups (Estonia: EEK before 2011-01-01, EUR after), locked-rate handling (DEM, FRF, etc. post-EUR), and ISO alias resolution.
* HTTP surface so consumers call across the cluster instead of mounting a shared PVC.

## API

| Method + path | Purpose |
|---|---|
| `POST /v1/parse` | Sentinel detection |
| `POST /v1/resolve` | Declared / country / date → effective currency |
| `POST /v1/convert` | Value + currency + date → EUR |
| `GET /v1/coverage` | Per-currency earliest+latest date we have a rate for |
| `GET /healthz` | Liveness (always 200 unless the python process is wedged) |
| `GET /readyz` | Readiness (503 until the loader has populated the PVC at least once) |
| `POST /v1/reload` | Token-gated; re-reads the PVC into memory (the loader cronjob calls this after a refresh) |

## Architecture

```
                 ┌────────────────────────┐
                 │  fontem-currency-loader │ (cronjob, daily 02:30 UTC)
                 │  python -m src.loader   │
                 └─────────┬───────────────┘
                           │ writes per-CCY JSON
                           ▼
                ┌─────────────────────────┐
                │  fontem-currency-rates  │ (RWX PVC)
                │  rates/USD.json ...     │
                └──────────┬──────────────┘
                           │ reads at boot + on POST /v1/reload
                           ▼
                ┌─────────────────────────┐
                │  fontem-currency (api)  │ ── HTTP ──▶  every consumer
                │  uvicorn, port 8080     │              (TED loader, stats, …)
                └─────────────────────────┘
```

## Layout

```
src/
  api/app.py            FastAPI surface
  loader/load.py        ECB + Frankfurter refresh, writes to PVC
  services/service.py   CurrencyService (in-memory rate cache)
  data/                 Static reference: locked_rates, country_currencies, aliases
tests/                  pytest, no network calls — fetchers are monkeypatched
deployment/             Helm chart (Deployment + Service + PVC + CronJob)
.gitea/workflows/ci.yml CI: pytest + pylint + build/sign/push
```

## Local dev

```
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
pylint src tests

# Local API (uses /tmp for the rates dir — no rates loaded → /readyz returns 503)
CURRENCY_DATA_DIR=/tmp/currency-data uvicorn src.api.app:app --reload

# Local loader (one-shot refresh of all currencies; takes ~2 min over WAN)
python -m src.loader.load --rates-dir /tmp/currency-data --start 2024-01-01
```
