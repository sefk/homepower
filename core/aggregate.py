"""Aggregation that respects coverage.

Every aggregate returns its coverage fraction alongside the number, so a
caller can never mistake "three days were missing" for "usage was low".
Nothing here fills gaps with zeros.
"""

from datetime import datetime, timedelta
from typing import NamedTuple

from .coverage import covered_fraction
from .models import Sample, Series


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
