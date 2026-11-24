> ### 🪞 This GitHub repository is a mirror
>
> Development happens on Fontem's own infrastructure; this mirror is
> updated automatically. **Issues and pull requests opened here are not
> monitored.**
>
> If you would like to contribute — code, data sources, review, or
> anything else — please get in touch at **team@fontem.eu** and we will
> set you up.

# fontem-currency

Currency reference + EUR conversion service. **Singleton — one deployment in the `currency-service` namespace serves every environment** (testing / staging / dast / prod) over cluster DNS. Mirrors the `fontem-linguistics` posture: non-critical, offline data sources, cluster-internal only.

## What it does

* Daily exchange rates from the ECB Statistical Data Warehouse (primary, ~30 currencies).
* Frankfurter as the fallback for currencies ECB doesn't carry (MDL, MKD, UAH, RSD, BAM, …).
* In-process `CurrencyService` with sentinel detection (TED's `-1` / `0.01` are "value undisclosed"), currency-history lookups (Estonia: EEK before 2011-01-01, EUR after), locked-rate handling (DEM, FRF, etc. post-EUR), and ISO alias resolution.
* HTTP surface so consumers call across the cluster instead of mounting a shared PVC.
* Prices quoted in a minor unit: GBp / GBX (pence), ZAc (cents), ILA (agorot) convert through their ISO currency. `/v1/convert` and `/v1/resolve` return that `currency` and the `scale` applied (0.01 for pence). The codes are in `src/data/minor_unit_codes.json`; the factor is the currency's minor units in the ISO 4217 list, bundled verbatim as `src/data/iso4217-list-one.xml` from SIX, the ISO 4217 maintenance agency (https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml). To refresh it, download that file over the bundled one; its `Pblshd` date is logged at start-up.

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
  services/units.py     Minor-unit codes (GBp, ZAc, ...)
  data/                 Static reference: locked_rates, country_currencies, aliases,
                        minor_unit_codes, the ISO 4217 list
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

## License

Apache License 2.0 — see [LICENSE](LICENSE).
