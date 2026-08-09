# Review insights

Staging area for mistake-*shapes* surfaced by code review — patterns that
would recur, not the one-off fixes themselves. When a theme reaches ~3
entries, promote it: a rule in the agent docs, an automated check, or a
tracked issue — then collapse the theme to a pointer.

## Gap/boundary semantics must have one authority

- 2026-08-09 — Coverage grace, chart gap thresholds, and energy
  aggregation each re-derived "what counts as a hole" independently and
  disagreed at every boundary: inverted spans on lookback re-reads,
  inclusive-vs-exclusive at exactly 2×, fixed durations under variable
  cadence, unclipped window straddles. Five consecutive Codex findings,
  one root cause. Resolution: charts now read `coverage.uncovered()`
  directly, grace is a per-collector property with documented exclusive
  semantics, aggregation clips overlap. Any new consumer of samples must
  derive gaps from coverage, never from timestamp arithmetic of its own.
  (collectors/base.py, catalog/views.py, core/aggregate.py; codex
  019fe569)

## DST is a standing adversary for local-time windows

- 2026-08-09 — Two independent findings in one night: SolarEdge's naive
  local timestamps collide on fall-back (fold handling), and every
  window built as `local midnight + timedelta(hours=h)` lies on
  transition days because same-tzinfo datetimes subtract by WALL CLOCK
  (a phantom spring-forward hour measured 3600 "seconds" and rendered
  $0.00). Resolution: coverage/aggregate entry points normalize to UTC
  (`core/coverage._utc`). Any new code doing arithmetic on two
  America/Los_Angeles datetimes should convert to UTC first — and TOU
  boundary logic (4pm, 9pm, seasons) is the one place local wall time is
  genuinely the right domain. (core/coverage.py, collectors/solaredge.py;
  codex 019fe569)
- 2026-08-09 — Third and fourth instances the same night: same-tzinfo
  *comparison* takes the wall-clock shortcut too (fold-blind — bit the
  Green Button end-time fix), and a fold latch that never releases
  corrupts the NEXT fall-back a year later. **At promotion threshold**:
  proposed rule for .claude/agents/dev.md pending owner go-ahead.
  (core/management/commands/import_greenbutton.py)

## Vendor SDK exceptions may not subclass Exception

- 2026-08-09 — tesla-fleet-api's TeslaFleetError subclasses
  BaseException; `except Exception` failure bookkeeping never sees it, so
  a sleeping car would have crashed the scheduler task instead of
  recording a failed run. Wrap vendor calls and re-raise as RuntimeError
  at the collector boundary. Check the exception hierarchy of every new
  vendor lib before trusting the base class's catch. (collectors/tesla.py)
