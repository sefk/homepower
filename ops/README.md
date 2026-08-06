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

## Notes

- `KeepAlive` restarts the process if either the web server or the
  collector side dies; `serve` deliberately exits whole when one half
  fails, so launchd restarts cleanly rather than running half-alive.
- Collector downtime is expected and healed later by backfill; see the
  coverage model in the PRD.
- Secrets live in `.env` (see top-level README); the launchd job reads
  nothing secret from the plist.
