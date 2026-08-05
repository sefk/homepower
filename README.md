# 635 Central Home Power Monitor

One Django process on `studio` that collects telemetry from the
property's four metering systems, stores it in SQLite at native
resolution, and serves an opinionated catalog of analyses on the LAN.
See the [PRD][prd] for the full brief and the design invariants
(gaps are unknown-not-zero, nothing interpolates, mixed resolution is
permanent).

Status: **Milestone 1** — foundation + Envoy (ADU solar). Coming per the
[build order][prd]: SolarEdge cloud, bill seeding, Tesla, Eagle 3.

## Setup

Requires [uv][uv]; everything else installs from `pyproject.toml`.

```sh
uv sync
uv run python manage.py migrate
```

Secrets go in `.env` (gitignored, never committed):

```sh
ENPHASE_USERNAME=you@example.com   # Enlighten cloud login; mints the local-API JWT
ENPHASE_PASSWORD=...
# optional overrides:
# ENVOY_HOST=10.10.0.222
# HOMEPOWER_PORT=8000
# DJANGO_DEBUG=true
```

A source without credentials simply doesn't run — its absence shows up
as unknown coverage on the health page, not as an error.

## Run

```sh
uv run python manage.py serve                # web UI + collectors, one process
uv run python manage.py serve --no-collect   # UI only (development)
uv run pytest                                # test suite
```

Then http://localhost:8425/ — the catalog:

- `/health/` — per-source coverage timeline, freshness, recent failures
- `/solar/` — ADU production, day/week, gaps rendered as gaps

For boot-time operation under `launchd`, see [ops/README.md][ops].

## Layout

| Path | What |
| --- | --- |
| `core/` | schema (samples at native resolution + coverage spans) and coverage/aggregation logic |
| `collectors/` | collector base, per-source scheduler, Envoy integration, `serve` command |
| `catalog/` | the analyses; each view states the question it answers |
| `ops/` | launchd plist and install notes |
| `docs/prd/homepower/` | PRD and discovery notes |
| `var/` | runtime state: logs, cached vendor tokens (gitignored) |

## Design notes

- **Coverage is explicit.** A `CoverageSpan` says an interval is `live`,
  `backfilled`, or `confirmed_empty`; absence of a span means unknown.
  Aggregates return their coverage fraction so a three-day outage can
  never masquerade as a low-usage month.
- **One process.** `manage.py serve` runs uvicorn and the collector
  scheduler in one asyncio loop. If either half dies the process exits
  so launchd restarts it whole.
- **Envoy token churn is normal.** Firmware D7+ requires an
  Enlighten-minted JWT for the local API; it's cached in `var/` and
  re-minted automatically when rejected.

[prd]: docs/prd/homepower/prd.md
[ops]: ops/README.md
[uv]: https://docs.astral.sh/uv/
