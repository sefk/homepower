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

## Tesla Fleet API (EV source)

See [tesla-setup.md](tesla-setup.md) — developer-app registration,
public-key hosting, partner-account registration, and the
`manage.py tesla_auth` refresh-token bootstrap.

## Green Button backfill (grid history before/beyond the Eagle 3)

PG&E's own meter history, for filling gaps the Eagle 3 didn't cover or
seeding data from before it was installed:

**pge.com → Energy Usage Details → Green Button "Export usage for a
range" → CSV.**

```sh
uv run python manage.py import_greenbutton ~/Downloads/pge_electric_usage_*.csv
```

Lands as `grid_import_wh` / `grid_export_wh` on the `eagle` Source,
coverage recorded `backfilled`. Re-running the same file is safe — rows
upsert on (series, timestamp).

## Notes

- `KeepAlive` restarts the process if either the web server or the
  collector side dies; `serve` deliberately exits whole when one half
  fails, so launchd restarts cleanly rather than running half-alive.
- Collector downtime is expected and healed later by backfill; see the
  coverage model in the PRD.
- Secrets live in `.env` (see top-level README); the launchd job reads
  nothing secret from the plist.
