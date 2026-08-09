"""Aggregation that respects coverage.

Every aggregate returns its coverage fraction alongside the number, so a
caller can never mistake "three days were missing" for "usage was low".
Nothing here fills gaps with zeros.
"""

from datetime import datetime
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


def energy_wh(series: Series, start: datetime, end: datetime) -> WindowEnergy:
    """Integrate power samples (W) over [start, end) into Wh.

    Sums only samples that exist; the coverage fraction tells the caller
    how much of the window that actually represents.
    """
    joules_per_hour = 0.0
    samples = Sample.objects.filter(series=series, ts__gte=start, ts__lt=end)
    for s in samples.iterator():
        joules_per_hour += s.value * s.duration_s
    return WindowEnergy(
        wh=joules_per_hour / 3600.0,
        coverage=covered_fraction(series, start, end),
    )


def energy_wh_split(series: Series, start: datetime, end: datetime) -> WindowEnergySplit:
    """Integrate a signed power series (grid demand) into import/export Wh."""
    pos = neg = 0.0
    samples = Sample.objects.filter(series=series, ts__gte=start, ts__lt=end)
    for s in samples.iterator():
        ws = s.value * s.duration_s
        if ws >= 0:
            pos += ws
        else:
            neg -= ws
    return WindowEnergySplit(
        imported_wh=pos / 3600.0,
        exported_wh=neg / 3600.0,
        coverage=covered_fraction(series, start, end),
    )
