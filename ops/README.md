# Operations

## launchd install (studio)

The service is a per-user LaunchAgent — it runs as `sefk`, starts at boot,
and needs no GUI login.

```sh
cp ops/com.sefk.homepower.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sefk.homepower.plist
```

Check it:

```sh
launchctl print gui/$(id -u)/com.sefk.homepower | head -20
curl -s http://localhost:8425/
```

Stop / restart:

```sh
launchctl kickstart -k gui/$(id -u)/com.sefk.homepower   # restart
launchctl bootout gui/$(id -u)/com.sefk.homepower        # stop + unload
```

## Logs

- `var/log/homepower.log` — application log (rotating, 5×10MB)
- `var/log/launchd.out.log`, `var/log/launchd.err.log` — process stdout/stderr
- `var/grafana/log/grafana.log` — Grafana; `var/log/grafana.{out,err}.log` for
  its stdout/stderr

## Grafana (live dashboards)

Grafana reads the same SQLite database directly and serves real-time
dashboards on **http://studio.local:3425/**. It only ever reads: the SQLite
datasource plugin forces `_pragma=query_only(1)`, so the Django process stays
the single writer. Nothing about the collectors changes to support it.

Install, once:

```sh
brew install grafana
grafana cli --homepath /opt/homebrew/opt/grafana/share/grafana \
  --pluginsDir "$PWD/var/grafana/plugins" \
  plugins install frser-sqlite-datasource

cp ops/com.sefk.grafana.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sefk.grafana.plist
```

The two panel plugins (a [Sankey][sankey] and an [hourly heatmap][heatmap])
and the datasource plugin are declared once, in `grafana.ini`
(`[plugins] preinstall_sync`), so a fresh install fetches them on first start
and the `plugins install` above is only a way to do it ahead of time. To add
them to an existing install without a restart-time download:

```sh
for p in netsage-sankey-panel marcusolsson-hourly-heatmap-panel; do
  grafana cli --homepath /opt/homebrew/opt/grafana/share/grafana \
    --pluginsDir "$PWD/var/grafana/plugins" plugins install $p
done
launchctl kickstart -k gui/$(id -u)/com.sefk.grafana   # plugins load at start
```

Same launchctl verbs as the app (`kickstart -k` to restart, `bootout` to
stop). Don't also run `brew services start grafana` — Homebrew's service
hardcodes `/opt/homebrew/etc/grafana.ini` and would fight this one for the
port.

Everything is config-as-code under `ops/grafana/`:

| Path | What |
| --- | --- |
| `grafana.ini` | port, anonymous LAN read access, paths into `var/grafana/`, plugin list |
| `provisioning/datasources/` | the SQLite datasource, pinned to `db.sqlite3` |
| `provisioning/dashboards/` | points Grafana at the dashboard directory |
| `dashboards/*.json` | the dashboards themselves |
| `check_panels.py` | runs every panel's SQL and reports failures |

Dashboards are provisioned read-only and re-read from disk every 30s, so
editing a JSON file is enough — no restart, no export step. The UI's edit
controls are disabled on purpose: a dashboard saved in the browser would be
state Grafana owns and git doesn't.

- **Sources** — where power comes from: each array and grid import, now
  and stacked over time; per-kW array comparison; monthly and yearly
  production over all backfilled history
- **Sinks** — where it goes: house load (derived as solar + grid) and
  export, the always-on floor, the 4–9pm share, each separately metered
  load on its own chart (the Tesla, the Mac Studio and the water heater;
  any non-solar/grid/gas power series joins automatically, and the Loads
  picker at the top chooses which to show; Aggregate averages them into
  15-minute to 1-day buckets, Auto going daily beyond 10 days), a load
  histogram, a typical-day profile and an hour-by-day heatmap of house
  load. Loads marked (estimated), like the hot tub, are derived from
  whole-home meter step changes rather than metered
- **Sink Summary** — a stacked breakdown of where the power goes
  (Unmetered at the bottom, each metered load above it, export on top)
  and a kWh/share table below it. "Where it goes" and "Sent to PG&E"
  choose the layers (by default every load but the Mac Studio, too small to
  see; a newly added load starts unchecked here); Aggregate averages the chart into 15-minute to 1-day
  buckets (Auto: full detail up to 10 days, daily beyond)
- **Sources and Sinks** — the two together: solar→house, solar→grid and
  grid→house energy, self-sufficiency and self-use, an energy-flow Sankey
  (kWh from each array and the grid to the house and back out), a mirrored balance
  chart, net use (import minus export, bars sized to the picked range),
  daily balance and a time-of-use cost estimate. A **Compare** row below
  ignores the picker: solar, house use and net by month year over year
  (this year in colour, last year lighter, earlier years grey), and this
  month to date against last month as a table and running totals
- **Live Power** (also the home dashboard) — solar vs. grid at native
  resolution, 10s refresh, today's kWh; then the current through the 200 A
  service (watts over a nominal 240 V, peak per point) against the main
  and its 160 A continuous rating, exports dipping below zero
- **Gas** — therms over the range (daily, weekly or monthly bars by
  range), summer baseline, and gas by month year over year. Daily
  readings, 1–2 days behind (PG&E's posting lag)
- **Data Health** — sample freshness, coverage percentage, poll outcomes
- **Energy** — kWh per hour and per local day, with the observed-time panel
  that says how much of each bucket was actually seen

The panels honour the same invariant the app does: a gap is drawn as a gap
(`insertNulls` breaks the line past ~3× a source's native resolution) and
energy totals only ever integrate stored samples, so an outage shows up as a
short bar next to an incomplete coverage bar rather than as a low-usage hour.

Live Power, Energy and Data Health discover series from the database rather
than hardcoding them, so new sources appear there on their own once their
collectors start writing. Sources, Sinks and Sources and Sinks name the
three sources (`solaredge`, `envoy`, `eagle`) in their SQL, because the
arithmetic between them is the point; a new source needs adding there by
hand. They open on the last 24 hours, and their daily and typical-day
panels always show the last 30 days. After editing any dashboard JSON:

```sh
python3 ops/grafana/check_panels.py    # every query, against the live instance
```

A broken query renders as an empty panel, which looks exactly like a data
gap — hence the checker.

Milestones — dated changes like a new appliance — show as dashed markers
on every time-axis chart (label on hover; the **Milestones** toggle hides
them). They live in the `core_milestone` table, so add or remove them
from the command line, not the dashboards:

```sh
uv run python manage.py milestone list
uv run python manage.py milestone add 2027-03-01 "Battery installed"
uv run python manage.py milestone remove 4
```

The year-over-year and month-over-month panels have no time axis, so
they can't carry markers.

## macOS Local Network privacy (one-time)

LAN access is granted per-binary on modern macOS. Your terminal has it,
but the launchd-run python is silently denied — every Envoy connection
fails with `errno 65, No route to host` while internet requests
succeed. (Apple's own binaries like `curl` are exempt, which makes this
maddening to diagnose.)

Fix, once, in the GUI: **System Settings → Privacy & Security →
Local Network**, enable the **python** / **uv** entry (it appears after
the service has attempted LAN access), then:

```sh
launchctl kickstart -k gui/$(id -u)/com.sefk.homepower
```

Expect to redo this if the python interpreter path changes (e.g. a uv
python upgrade).

## Backups

`manage.py backup_db` writes a gzipped SQLite online backup (consistent
while the collectors write; ~37 MB, ~4 s) to
`/Volumes/ext1/homepower_backups/homepower-YYYY-MM-DD.sqlite3.gz`, then
prunes: every backup from the last 7 days, plus the first of each ISO week
for a year. It refuses to run if the directory is missing, so an
unmounted drive fails the job instead of filling the boot disk. A
LaunchAgent runs it daily at 3:30am (a missed run fires on wake):

```sh
cp ops/com.sefk.homepower-backup.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sefk.homepower-backup.plist
launchctl kickstart gui/$(id -u)/com.sefk.homepower-backup   # run now
```

Output goes to `var/log/backup.{out,err}.log`.

Like Local Network above, external-drive access is granted per binary:
the launchd-run python hangs in `open()` on `/Volumes/ext1` until it is
allowed. **System Settings → Privacy & Security → Files & Folders** →
python → **Removable Volumes** (or approve the prompt when it appears).
Redo after a python path change.

Restore: stop the service, `gunzip -c <backup> > db.sqlite3`, delete any
`db.sqlite3-wal`/`-shm` left beside it, start the service.

## Eagle 3 uploader (grid source)

The Eagle (10.10.0.216) pushes RFA XML to `POST /ingest/eagle/` via a
"custom uploader" configured over its local API. Its network-setup web
UI has **no uploader page**; the uploader command set lives behind
`/cgi-bin/post_manager` (basic auth: Cloud ID / Install Code, both in
`.env` from the label on the unit). Commands need a unique `<Id>`.

Registered with (note the **doubled leading slash** — the firmware
strips one, and without a stored leading slash it builds a broken URL
and fails with `LastResponseCode 0`, silently):

```sh
source .env
curl -u "$EAGLE_CLOUD_ID:$EAGLE_INSTALL_CODE" -H "Content-Type: text/xml" \
  -d "<Command><Name>uploader_add</Name><Id>0x$(date +%s)</Id>\
<uploader>homepower</uploader><provider>homepower</provider>\
<description>homepower on studio</description><format>XML:RAW</format>\
<hostname>10.10.0.200</hostname><url>//ingest/eagle/</url><port>8425</port>\
<enabled>Y</enabled><uploadSize>0</uploadSize><protocol>http</protocol>\
<compression>N</compression><encode>N</encode><UploadPeriod></UploadPeriod>\
<autoselect>true</autoselect></Command>" \
  http://10.10.0.216/cgi-bin/post_manager
```

`uploadSize 0` = streaming (a push per meter report, ~8s).
Inspect with `uploader_list` (check `LastSent` / `LastResponseCode`),
remove with `uploader_delete` + `<provider>homepower</provider>`.
Command names and parameters were recovered from the Rainforest cloud
portal's JS bundle; they are not publicly documented.

## PG&E gas (daily, automatic)

The `pge_gas` collector signs in to pge.com with `PGE_USERNAME` /
`PGE_PASSWORD` (through the [opower][opower] library Home Assistant uses)
every 6 hours and re-reads the last 30 days of daily gas use, so late or
corrected days fill in by themselves. PG&E posts each day 1–2 days late.

PG&E asks for a texted or emailed code the first time a device signs in.
Do that once, interactively:

```sh
uv run python manage.py pge_auth
launchctl kickstart -k gui/$(id -u)/com.sefk.homepower
```

It saves the remembered-device cookie to `var/pge_login.json` and says
when PG&E means it to expire. When PG&E forgets it, `/health/` shows
`pge_gas` failing with "run `manage.py pge_auth`" — rerun it, no restart
needed. It uses the website's login rather than an official API, so a
pge.com change can break it until opower catches up (`uv lock
--upgrade-package opower`); the Green Button import below still works
for gaps.

## Tesla Fleet API (EV source)

See [tesla-setup.md](tesla-setup.md) — developer-app registration,
public-key hosting, partner-account registration, and the
`manage.py tesla_auth` refresh-token bootstrap.

## Green Button backfill (grid history before/beyond the Eagle 3, and gas)

PG&E's own meter history, for filling gaps the Eagle 3 didn't cover or
seeding data from before it was installed:

**pge.com → My Usage → Energy Usage Details → the green "Download my
data" button → Export usage for a range → CSV.** One export covers at
most about a year, so step backwards a year at a time. The zip holds
electric *and* gas files; import both — the importer tells them apart by
the `USAGE (therms)` header.

macOS keeps the terminal out of `~/Downloads` unless it has been granted
access, so copy the files somewhere readable (e.g. `.tmp/pge/`) first:

```sh
uv run python manage.py import_greenbutton .tmp/pge/pge_*_usage_*.csv
```

Electric lands as hourly `grid_import_wh` / `grid_export_wh` on the `eagle`
Source, coverage recorded `backfilled`. Re-running the same file is
safe — rows upsert on (series, timestamp). The Grafana dashboards use it
wherever the Eagle has no reading, spread evenly over each hour's
quarters and paired with that hour's mean solar, so house load before
the Eagle was installed shows as hourly steps.

Gas lands as daily `gas_wh` on its own `pge_gas` Source (kind `gas`),
converted at 29,307.1 Wh per therm so it shares a unit with everything
else; the Gas dashboard turns it back into therms. The `pge_gas`
collector writes the same series the same way, so a hand import and a
poll of the same day agree; imports are now only for history older than
its 30-day window.

## PG&E TOU rates (daily, automatic)

Costs on Peak, Cost map, Grid and EV price each hour at an effective-dated
all-in $/kWh from two tables (`billing.UtilityRate` + `billing.CcaAdjustment`,
logic in `billing/rates.py`):

- **UtilityRate**, PG&E's side: kept current by the `pge_rates` collector.
  Once a day it signs in as `pge_gas` does (same `pge_auth` cookie), reads
  30 days of hourly electric cost from Opower, derives cost / kWh per
  season, peak/off-peak and tier, and inserts a row effective the first
  day a rate moves by more than $0.0005. A change is logged at WARNING and
  appears in the run message on `/health/` ("RATE CHANGE: ..."). To
  recover older change dates: `manage.py pge_rates_backfill --days 400
  --dry-run` (prints the rate eras and the rows it would write; drop
  `--dry-run` to write). Days before 2026-03-01 are ignored: earlier
  history is all-in values back-derived from bills.
- **CcaAdjustment**: WestLight generation - PG&E generation credit + PCIA,
  $/kWh. Opower can't see it, so enter it from a bill, effective on the
  bill period's start date:

  ```sh
  uv run python manage.py set_cca_adjustment winter peak 2026-10-01 0.0816 --note "Nov 2026 bill"
  ```

  Until it exists, `/grid/` and `/health/` warn and winter is priced on
  PG&E's side only. (Winter has no adjustment yet; summer has the July
  2026 one.) The seeded 2026-03-01 winter rates are provisional until a
  backfill dates them.

Winter prices at tier 2 when PG&E reports a tier-2 rate for the date,
otherwise tier 1; summer is always tier 1.

## Notes

- `KeepAlive` restarts the process if either the web server or the
  collector side dies; `serve` deliberately exits whole when one half
  fails, so launchd restarts cleanly rather than running half-alive.
- Collector downtime is expected and healed later by backfill; see the
  coverage model in the PRD.
- Secrets live in `.env` (see top-level README); the launchd job reads
  nothing secret from the plist.

[sankey]: https://grafana.com/grafana/plugins/netsage-sankey-panel/
[heatmap]: https://grafana.com/grafana/plugins/marcusolsson-hourly-heatmap-panel/
[opower]: https://github.com/tronikos/opower
