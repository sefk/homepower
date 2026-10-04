# 635 Central Home Power Monitor

One Django process on `studio` that collects telemetry from the
property's four metering systems, stores it in SQLite at native
resolution, and serves an opinionated catalog of analyses on the LAN.
See the [PRD][prd] for the full brief and the design invariants
(gaps are unknown-not-zero, nothing interpolates, mixed resolution is
permanent).

Status: **Milestone 1** (foundation + Envoy ADU solar) plus the
**Eagle 3 grid source** — whole-home import/export pushed by the meter
every ~8s. The **SolarEdge cloud collector** (main array, 15-min) and
the **Tesla Fleet API collector** (EV charging; polls slowly while
idle and caps its monthly requests to stay inside Tesla's $10 API
credit) ship key-gated — each stays disabled until its `.env`
credentials are set.
Coming per the [build order][prd]: peak decomposition.

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
SOLAREDGE_USERNAME=you@example.com # monitoring.solaredge.com portal login (no API key needed)
SOLAREDGE_PASSWORD=...
SOLAREDGE_SITE_ID=...              # number in the portal URL after signing in
TESLA_CLIENT_ID=...                # see ops/tesla-setup.md
TESLA_CLIENT_SECRET=...
TESLA_REFRESH_TOKEN=...            # from `manage.py tesla_auth` (after `tesla_register`)
PGE_USERNAME=you@example.com       # pge.com login, for gas; then run `manage.py pge_auth` once
PGE_PASSWORD=...
# optional overrides:
# ENVOY_HOST=10.10.0.222
# HOMEPOWER_PORT=8425
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

Then http://localhost:8425/ — the analysis catalog index, every view
listed with the question it answers. Highlights:

- `/health/` — per-source coverage timeline, freshness, recent failures
- `/solar/`, `/grid/` — production and demand at native resolution,
  gaps rendered as gaps
- `/trueup/`, `/peak/`, `/costmap/` — where the cycle is heading, what
  the 4–9pm window costs, which hours cost the money
- `/baseline/`, `/selfuse/`, `/solarhealth/`, `/electrify/` — overnight
  floor trend, self-consumption, per-kW array comparison, gas-to-heat-
  pump modeling
- `/ev/` — Tesla charge sessions: kWh, actual cost, and what shifting
  the session past 9pm would have cost instead

A Grafana instance on **http://localhost:3425/** serves live dashboards off
the same SQLite database, read-only — Sources, Sinks, Sources and Sinks,
Live Power (10s refresh), Energy, Gas, and Data Health. It's config-as-code in `ops/grafana/`; install and operation are
in [ops/README.md][ops].

The Eagle 3 pushes to `POST /ingest/eagle/`; see [ops/README.md][ops]
for configuring its uploader. Historical grid data and daily gas use
import from PG&E Green Button CSVs via `manage.py import_greenbutton`
(backfilled coverage; see ops/README.md).

Solar history comes from the vendor clouds: `manage.py backfill` walks
SolarEdge (15-min, back to the 2015 install) and Enphase Enlighten
(15-min, back to the ADU array's first day) and stores it as
`backfilled` coverage. It is idempotent and never overwrites intervals
the collectors saw live, so it is also how downtime gets healed:

```sh
uv run python manage.py backfill                      # everything, all history
uv run python manage.py backfill envoy --start 2026-09-01 --end 2026-09-07
```

For boot-time operation under `launchd`, see [ops/README.md][ops].

## Layout

| Path | What |
| --- | --- |
| `core/` | schema (samples at native resolution + coverage spans) and coverage/aggregation logic |
| `collectors/` | collector base, per-source scheduler, Envoy integration, `serve` command |
| `catalog/` | the analyses; each view states the question it answers |
| `ops/` | launchd plists, install notes, Grafana config and dashboards |
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
- **Grafana is a reader, not a second writer.** It opens `db.sqlite3` with
  `query_only`, so the collector process keeps sole write access and the
  dashboards can't perturb what they're measuring. Its own state lives in
  `var/grafana/`; the dashboards live in git.
- **Tesla refresh tokens rotate on every use.** Like the Envoy JWT, the
  current one is cached in `var/tesla_token.json` and updated after
  every poll, so a restart doesn't need `manage.py tesla_auth` re-run.

[prd]: docs/prd/homepower/prd.md
[ops]: ops/README.md
[uv]: https://docs.astral.sh/uv/
