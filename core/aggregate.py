"""Aggregation that respects coverage.

Every aggregate returns its coverage fraction alongside the number, so a
caller can never mistake "three days were missing" for "usage was low".
Nothing here fills gaps with zeros.
"""

from datetime import datetime, timedelta
from typing import NamedTuple

from django.db.models import Max

from .coverage import covered_fraction
from .models import Sample, Series, Source


class WindowEnergy(NamedTuple):
    wh: float  # energy from the samples that exist
    coverage: float  # fraction of the window with known status, [0, 1]


class WindowEnergySplit(NamedTuple):
    imported_wh: float  # energy while the signed series was positive
    exported_wh: float  # energy while it was negative (as a positive number)
    coverage: float


# Widest sample interval any collector produces (SolarEdge quarters).
# Bounds the lookback for samples that start before a window but overlap it.
_MAX_SAMPLE_S = 3600


def _overlapping_watt_seconds(series: Series, start: datetime, end: datetime):
    """Yield value * seconds-of-overlap for samples intersecting [start, end).

    A sample covers [ts, ts + duration); only the part inside the window
    counts. Without clipping, a stretched or straddling sample charges its
    whole duration to the window containing its start — inflating one
    aggregation bucket and deflating its neighbor.
    """
    from .coverage import _utc

    start, end = _utc(start), _utc(end)
    samples = Sample.objects.filter(
        series=series,
        ts__gte=start - timedelta(seconds=_MAX_SAMPLE_S),
        ts__lt=end,
    )
    for s in samples.iterator():
        s_end = s.ts + timedelta(seconds=s.duration_s)
        overlap = (min(end, s_end) - max(start, s.ts)).total_seconds()
        if overlap > 0:
            yield s.value * overlap


def energy_wh(series: Series, start: datetime, end: datetime) -> WindowEnergy:
    """Integrate power samples (W) over [start, end) into Wh.

    Sums only samples that exist; the coverage fraction tells the caller
    how much of the window that actually represents.
    """
    joules = sum(_overlapping_watt_seconds(series, start, end))
    return WindowEnergy(
        wh=joules / 3600.0,
        coverage=covered_fraction(series, start, end),
    )


def energy_wh_split(series: Series, start: datetime, end: datetime) -> WindowEnergySplit:
    """Integrate a signed power series (grid demand) into import/export Wh."""
    pos = neg = 0.0
    for ws in _overlapping_watt_seconds(series, start, end):
        if ws >= 0:
            pos += ws
        else:
            neg -= ws
    return WindowEnergySplit(
        imported_wh=pos / 3600.0,
        exported_wh=neg / 3600.0,
        coverage=covered_fraction(series, start, end),
    )


# Below this, demand_w is "complete enough" to integrate directly rather
# than fall back to the Green Button interval series.
GRID_DEMAND_MIN_COVERAGE = 0.99


def _clipped_energy_wh(series: Series, start: datetime, end: datetime) -> float:
    """Sum energy (Wh) samples overlapping [start, end), clipped by overlap.

    Unlike _overlapping_watt_seconds (power samples, clipped by
    overlap-seconds), these rows already hold interval energy — a Green
    Button row's value is the whole quarter/hour's Wh, not a rate. Clip
    proportionally instead: value * overlap_s / duration_s.
    """
    from .coverage import _utc

    start, end = _utc(start), _utc(end)
    # Green Button rows can carry any duration up to daily granularity
    # (unlike every other collector's fixed sub-hour cadence), so the
    # fixed _MAX_SAMPLE_S lookback used elsewhere would miss a sample that
    # started long before `start` but still overlaps the window -- a
    # window inside such a row would read zero energy despite full
    # coverage. Recomputed per call (one cheap aggregate at our volumes)
    # rather than assumed, since this series has no recorded native
    # resolution the way Source.native_resolution_s gives other sources.
    lookback_s = (
        series.samples.aggregate(Max("duration_s"))["duration_s__max"] or _MAX_SAMPLE_S
    )
    total = 0.0
    samples = Sample.objects.filter(
        series=series,
        ts__gte=start - timedelta(seconds=lookback_s),
        ts__lt=end,
    )
    for s in samples.iterator():
        s_end = s.ts + timedelta(seconds=s.duration_s)
        overlap = (min(end, s_end) - max(start, s.ts)).total_seconds()
        if overlap > 0 and s.duration_s > 0:
            total += s.value * overlap / s.duration_s
    return total


def _grid_energy_series_split(start: datetime, end: datetime) -> WindowEnergySplit:
    """grid_import_wh/grid_export_wh (Green Button backfill) for [start, end)."""
    import_series = (
        Series.objects.filter(source__kind=Source.Kind.GRID, metric="grid_import_wh").first()
    )
    export_series = (
        Series.objects.filter(source__kind=Source.Kind.GRID, metric="grid_export_wh").first()
    )
    imported_wh = _clipped_energy_wh(import_series, start, end) if import_series else 0.0
    exported_wh = _clipped_energy_wh(export_series, start, end) if export_series else 0.0
    coverages = [
        covered_fraction(s, start, end) for s in (import_series, export_series) if s is not None
    ]
    return WindowEnergySplit(
        imported_wh=imported_wh,
        exported_wh=exported_wh,
        coverage=min(coverages) if coverages else 0.0,
    )


def grid_hourly_wh(start: datetime, end: datetime) -> WindowEnergySplit:
    """Import/export Wh for [start, end) — the grid source, whichever exists.

    demand_w (Eagle instantaneous demand) is used directly once its
    coverage for the window clears GRID_DEMAND_MIN_COVERAGE — it's live
    and finer-grained. Below that, this compares demand_w's own
    (partial) coverage against grid_import_wh/grid_export_wh (Green
    Button backfill) and returns whichever is more complete for this
    window, rather than an all-or-nothing switch: a window with some
    real demand samples but no Green Button data at all must still
    report what demand_w actually saw, not fall through to a
    nonexistent series and report zero coverage. Ties go to demand_w.
    Coverage in the result always describes whichever source was used.
    """
    demand_series = (
        Series.objects.filter(source__kind=Source.Kind.GRID, metric="demand_w").first()
    )
    demand_coverage = covered_fraction(demand_series, start, end) if demand_series else 0.0
    if demand_coverage >= GRID_DEMAND_MIN_COVERAGE:
        return energy_wh_split(demand_series, start, end)

    fallback = _grid_energy_series_split(start, end)
    if fallback.coverage > demand_coverage:
        return fallback
    if demand_series is not None:
        return energy_wh_split(demand_series, start, end)
    return fallback


def has_grid_data() -> bool:
    """Any grid Series at all -- demand_w (Eagle) or the Green Button
    grid_import_wh/grid_export_wh backfill. Existence gate for
    grid_hourly_wh's consumers: they used to check "does demand_w exist"
    before Green Button import could seed grid data with no Eagle 3
    involved at all.
    """
    return Series.objects.filter(source__kind=Source.Kind.GRID).exists()
