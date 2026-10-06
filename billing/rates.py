"""TOU rates for PG&E E-TOU-C under NEM, as all-in $/kWh.

The tariff, confirmed from the July 2026 bill and explained on the Grid
page (templates/catalog/grid.html): rate schedule E-TOU-C, "Time-of-Use
(Peak Pricing 4 - 9 p.m. Every Day)", enrolled in Net Energy Metering (NEM) -- not the Net Billing Tariff.
Under NEM, exports net against imports kWh-for-kWh within each TOU
period, so an exported kWh is worth what an imported one costs in the
same window. That is why one rate per (season, period) serves both
directions here, with no separate export credit.

Rates are effective-dated: in March 2026 PG&E moved ~$24/mo of fixed
cost into a Base Services Charge and lowered per-kWh rates, so a
2025 sample and a 2026 sample at the same hour are priced differently.
"""

from dataclasses import dataclass
from datetime import date, datetime

from django.utils import timezone

PEAK_START_HOUR = 16  # 4pm local
PEAK_END_HOUR = 21  # peak window ends 9pm local (exclusive)


@dataclass(frozen=True)
class RateTable:
    effective: date
    summer_peak: float
    summer_offpeak: float
    winter_peak: float
    winter_offpeak: float
    source: str


# July 2026 bill (06/15-07/14/2026, 171.0329 net kWh exported), summer.
# All-in = PG&E bundled net-usage rate - baseline credit - PG&E's
# generation credit (WestLight supplies generation instead) + PCIA +
# WestLight's generation rate. The bill gives the generation credit and
# PCIA only as dollar totals, so they're spread evenly per kWh; WestLight
# billed two rates mid-cycle, averaged here. The baseline credit applies
# to net usage under the 9.8 kWh/day allowance -- every summer kWh, since
# summer months net-export. Check: the bill's 9.96 peak + 161.07 off-peak
# net kWh at these rates = $54.22, vs. $54.19 billed (PG&E + WestLight,
# less the franchise fee and WestLight's $0.01/kWh net generation bonus).
BILL_2026_07_NET_KWH = 171.0329
PGE_SUMMER_PEAK_2026 = 0.52240
PGE_SUMMER_OFFPEAK_2026 = 0.39940
BASELINE_CREDIT_2026 = 0.08140
PGE_GENERATION_CREDIT_2026 = 17.23 / BILL_2026_07_NET_KWH
PCIA_2026 = 6.31 / BILL_2026_07_NET_KWH
WESTLIGHT_SUMMER_PEAK_2026 = (0.14048 + 0.15036) / 2
WESTLIGHT_SUMMER_OFFPEAK_2026 = (0.04778 + 0.05251) / 2

_ADJUSTMENTS_2026 = -BASELINE_CREDIT_2026 - PGE_GENERATION_CREDIT_2026 + PCIA_2026

# Back-derived from the 2025-26 bills (docs/635-central-energy-analysis-
# gemini.md §4C), before the Base Services Charge.
PRE_2026_SUMMER_PEAK = 0.66
PRE_2026_SUMMER_OFFPEAK = 0.45
PRE_2026_WINTER_PEAK = 0.625
PRE_2026_WINTER_OFFPEAK = 0.571

SUMMER_PEAK = PGE_SUMMER_PEAK_2026 + _ADJUSTMENTS_2026 + WESTLIGHT_SUMMER_PEAK_2026
SUMMER_OFFPEAK = PGE_SUMMER_OFFPEAK_2026 + _ADJUSTMENTS_2026 + WESTLIGHT_SUMMER_OFFPEAK_2026
# No post-March-2026 winter bill yet: winter carries the pre-2026 values
# forward, which likely overstates them. Replace from a Nov 2026+ bill.
WINTER_PEAK = PRE_2026_WINTER_PEAK
WINTER_OFFPEAK = PRE_2026_WINTER_OFFPEAK

RATE_TABLES = [
    RateTable(
        effective=date.min,
        summer_peak=PRE_2026_SUMMER_PEAK,
        summer_offpeak=PRE_2026_SUMMER_OFFPEAK,
        winter_peak=PRE_2026_WINTER_PEAK,
        winter_offpeak=PRE_2026_WINTER_OFFPEAK,
        source="Back-derived from 2025-26 bills",
    ),
    RateTable(
        effective=date(2026, 3, 1),
        summer_peak=SUMMER_PEAK,
        summer_offpeak=SUMMER_OFFPEAK,
        winter_peak=WINTER_PEAK,
        winter_offpeak=WINTER_OFFPEAK,
        source="Summer from the July 2026 bill; winter carried forward (no winter bill yet)",
    ),
]


def is_peak(local_dt: datetime) -> bool:
    """4pm-9pm local, every day (E-TOU-C: peak pricing every day of the week)."""
    return PEAK_START_HOUR <= local_dt.hour < PEAK_END_HOUR


def is_summer(d: date) -> bool:
    """June through September."""
    return 6 <= d.month <= 9


def table_for(d: date) -> RateTable:
    """The rate table in effect on `d`."""
    return [t for t in RATE_TABLES if t.effective <= d][-1]


def rates_for(d: date) -> tuple[float, float]:
    """(peak $/kWh, off-peak $/kWh) in effect on `d`.

    The one place date->rate mapping lives, so per-period cost
    estimates (peak view) and per-instant lookups (rate_for) can't drift
    apart from each other.
    """
    t = table_for(d)
    return (t.summer_peak, t.summer_offpeak) if is_summer(d) else (t.winter_peak, t.winter_offpeak)


def rate_for(dt: datetime) -> float:
    """$/kWh for an aware datetime, converted to house-local time first."""
    local_dt = timezone.localtime(dt)
    peak_rate, offpeak_rate = rates_for(local_dt.date())
    return peak_rate if is_peak(local_dt) else offpeak_rate
