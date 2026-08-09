"""Bill-derived TOU rate approximations.

These are back-derived from the historical bills (docs/635-central-
energy-analysis-gemini.md §4C), not the PG&E/PCE tariff sheets. They're
close enough for the catalog's cost views; per the PRD assumption, if
they drift from actual bills this needs a real rate table with NEM
credit rules instead.
"""

from datetime import date, datetime

from django.utils import timezone

SUMMER_PEAK = 0.66
SUMMER_OFFPEAK = 0.45
WINTER_PEAK = 0.625
WINTER_OFFPEAK = 0.571

PEAK_START_HOUR = 16  # 4pm local
PEAK_END_HOUR = 21  # peak window ends 9pm local (exclusive)


def is_peak(local_dt: datetime) -> bool:
    """4pm-9pm local, every day (PRD §2: peak pricing every day of the week)."""
    return PEAK_START_HOUR <= local_dt.hour < PEAK_END_HOUR


def is_summer(d: date) -> bool:
    """June through September."""
    return 6 <= d.month <= 9


def rates_for(d: date) -> tuple[float, float]:
    """(peak $/kWh, off-peak $/kWh) for the season `d` falls in.

    The one place season->rate mapping lives, so per-period cost
    estimates (peak view) and per-instant lookups (rate_for) can't drift
    apart from each other.
    """
    return (SUMMER_PEAK, SUMMER_OFFPEAK) if is_summer(d) else (WINTER_PEAK, WINTER_OFFPEAK)


def rate_for(dt: datetime) -> float:
    """$/kWh for an aware datetime, converted to house-local time first."""
    local_dt = timezone.localtime(dt)
    peak_rate, offpeak_rate = rates_for(local_dt.date())
    return peak_rate if is_peak(local_dt) else offpeak_rate
