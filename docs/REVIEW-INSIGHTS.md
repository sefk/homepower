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
