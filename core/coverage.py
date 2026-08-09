"""Coverage bookkeeping.

Everything here treats intervals as half-open [start, end). The invariant
served: an instant inside some span has known status; an instant outside
every span is unknown — not zero, not empty, unknown.
"""

from datetime import datetime, timezone as dt_timezone

from .models import CoverageSpan, Series


def _utc(dt: datetime) -> datetime:
    """Normalize to UTC at the boundary.

    Same-tzinfo aware datetimes subtract by WALL CLOCK in Python, so a
    caller passing two America/Los_Angeles datetimes across a DST edge
    gets fictitious durations (a phantom spring-forward hour "lasts" 3600
    wall seconds but zero real ones). Converting here makes every
    duration and comparison in this module real-time, whatever tz the
    caller built its windows in.
    """
    return dt.astimezone(dt_timezone.utc)


def record_coverage(
    series: Series, start: datetime, end: datetime, state: str
) -> CoverageSpan:
    """Record that [start, end) has known status `state`.

    Overlapping or touching spans of the same series+state are merged into
    one row, so a live collector extends a single span instead of accreting
    one row per poll. Spans of a different state are left alone — a
    backfilled span next to a live span stays two spans.
    """
    start, end = _utc(start), _utc(end)
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


def clear_coverage(series: Series, start: datetime, end: datetime, state: str) -> None:
    """Remove [start, end) from `state` spans, revising rather than voiding.

    A span that only partly overlaps [start, end) is trimmed, not deleted:
    one extending left of `start` keeps [span.start, start); one extending
    right of `end` keeps [end, span.end); one straddling both sides splits
    into both pieces. A span fully inside [start, end) is deleted outright.
    Spans of any other state are untouched — this revises what `state`
    alone claims to know (e.g. a corrected backfill re-import), never a
    collector's own LIVE knowledge.
    """
    start, end = _utc(start), _utc(end)
    if end <= start:
        raise ValueError(f"empty or inverted span: {start} .. {end}")

    affected = CoverageSpan.objects.filter(
        series=series, state=state, start__lt=end, end__gt=start
    )
    for span in list(affected):
        left_trim = span.start < start
        right_trim = span.end > end
        if left_trim and right_trim:
            CoverageSpan.objects.create(series=series, start=span.start, end=start, state=state)
            CoverageSpan.objects.create(series=series, start=end, end=span.end, state=state)
            span.delete()
        elif left_trim:
            span.end = start
            span.save(update_fields=["end"])
        elif right_trim:
            span.start = end
            span.save(update_fields=["start"])
        else:
            span.delete()


def spans(series: Series, start: datetime, end: datetime) -> list[CoverageSpan]:
    """Coverage spans intersecting [start, end), ordered by start."""
    start, end = _utc(start), _utc(end)
    return list(
        CoverageSpan.objects.filter(
            series=series, start__lt=end, end__gt=start
        ).order_by("start")
    )


def uncovered(series: Series, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """The unknown intervals within [start, end) — the gaps.

    Any span state counts as known: confirmed_empty is knowledge too.
    """
    start, end = _utc(start), _utc(end)
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
    start, end = _utc(start), _utc(end)
    window = (end - start).total_seconds()
    if window <= 0:
        return 0.0
    missing = sum((g1 - g0).total_seconds() for g0, g1 in uncovered(series, start, end))
    return 1.0 - missing / window
