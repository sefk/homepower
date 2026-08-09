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


def rate_for(dt: datetime) -> float:
    """$/kWh for an aware datetime, converted to house-local time first."""
    local_dt = timezone.localtime(dt)
    peak = is_peak(local_dt)
    if is_summer(local_dt):
        return SUMMER_PEAK if peak else SUMMER_OFFPEAK
    return WINTER_PEAK if peak else WINTER_OFFPEAK
