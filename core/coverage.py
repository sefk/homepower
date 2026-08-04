"""Coverage bookkeeping.

Everything here treats intervals as half-open [start, end). The invariant
served: an instant inside some span has known status; an instant outside
every span is unknown — not zero, not empty, unknown.
"""

from datetime import datetime

from .models import CoverageSpan, Series


def record_coverage(
    series: Series, start: datetime, end: datetime, state: str
) -> CoverageSpan:
    """Record that [start, end) has known status `state`.

    Overlapping or touching spans of the same series+state are merged into
    one row, so a live collector extends a single span instead of accreting
    one row per poll. Spans of a different state are left alone — a
    backfilled span next to a live span stays two spans.
    """
    if end <= start:
        raise ValueError(f"empty or inverted span: {start} .. {end}")

    neighbors = CoverageSpan.objects.filter(
        series=series, state=state, start__lte=end, end__gte=start
    )
    for span in neighbors:
        start = min(start, span.start)
        end = max(end, span.end)
    neighbors.delete()
    return CoverageSpan.objects.create(series=series, start=start, end=end, state=state)


def spans(series: Series, start: datetime, end: datetime) -> list[CoverageSpan]:
    """Coverage spans intersecting [start, end), ordered by start."""
    return list(
        CoverageSpan.objects.filter(
            series=series, start__lt=end, end__gt=start
        ).order_by("start")
    )


def uncovered(series: Series, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """The unknown intervals within [start, end) — the gaps.

    Any span state counts as known: confirmed_empty is knowledge too.
    """
    gaps = []
    cursor = start
    for span in spans(series, start, end):
        if span.start > cursor:
            gaps.append((cursor, span.start))
        cursor = max(cursor, span.end)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def covered_fraction(series: Series, start: datetime, end: datetime) -> float:
    """Fraction of [start, end) with known status, in [0, 1]."""
    window = (end - start).total_seconds()
    if window <= 0:
        return 0.0
    missing = sum((g1 - g0).total_seconds() for g0, g1 in uncovered(series, start, end))
    return 1.0 - missing / window
