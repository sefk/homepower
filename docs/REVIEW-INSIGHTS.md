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
