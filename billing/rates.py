"""TOU rates for PG&E E-TOU-C under NEM, as all-in $/kWh.

The tariff, confirmed from the July 2026 bill and explained on the Grid
page (templates/catalog/grid.html): rate schedule E-TOU-C, "Time-of-Use
(Peak Pricing 4 - 9 p.m. Every Day)", enrolled in Net Energy Metering (NEM) -- not the Net Billing Tariff.
Under NEM, exports net against imports kWh-for-kWh within each TOU
period, so an exported kWh is worth what an imported one costs in the
same window. That is why one rate per (season, period) serves both
directions here, with no separate export credit.

Rates live in the database, effective-dated, in two halves:

  * UtilityRate: PG&E's side, as Opower reports it (net-usage rate minus
    baseline credit for tier 1). The `pge_rates` collector keeps these
    current. Pre-March-2026 rows are the exception: back-derived all-in
    values (source "seed") that already include the CCA side.
  * CcaAdjustment: everything Opower doesn't see -- WestLight generation,
    less PG&E's generation credit, plus PCIA. Entered by hand from bills
    (`manage.py set_cca_adjustment`); zero in the seeded all-in era.

all-in = UtilityRate + CcaAdjustment, each the latest row effective on
the date. In March 2026 PG&E moved ~$24/mo of fixed cost into a Base
Services Charge and lowered per-kWh rates, so a 2025 sample and a 2026
sample at the same hour are priced differently.

Tier: E-TOU-C has a baseline allowance; kWh past it bill at tier 2. Hourly
data can't say which kWh are past the allowance, so this is an
approximation: summer is tier 1 (summer months net-export, staying under
the allowance), winter uses tier 2 when a tier-2 rate exists for the
date, since the marginal winter kWh is the one a decision changes.
"""

import bisect
import time
from dataclasses import dataclass
from datetime import date, datetime

from django.utils import timezone

PEAK_START_HOUR = 16  # 4pm local
PEAK_END_HOUR = 21  # peak window ends 9pm local (exclusive)

# First date the all-in = UtilityRate + CcaAdjustment decomposition holds.
# Before it, seeded rows are all-in on their own. The pge_rates collector
# ignores Opower days before this so it can't overwrite that history with
# PG&E-only numbers.
OPOWER_BASIS_START = date(2026, 3, 1)

SEASONS = ("summer", "winter")
PERIODS = ("peak", "offpeak")
SEASON_PERIODS = [(s, p) for s in SEASONS for p in PERIODS]

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
# Kept for the Grid page's build-up table; the seeded migration
# (billing/migrations/0002) carries the same arithmetic as literals.
BILL_2026_07_NET_KWH = 171.0329
PGE_SUMMER_PEAK_2026 = 0.52240
PGE_SUMMER_OFFPEAK_2026 = 0.39940
BASELINE_CREDIT_2026 = 0.08140
PGE_GENERATION_CREDIT_2026 = 17.23 / BILL_2026_07_NET_KWH
PCIA_2026 = 6.31 / BILL_2026_07_NET_KWH
WESTLIGHT_SUMMER_PEAK_2026 = (0.14048 + 0.15036) / 2
WESTLIGHT_SUMMER_OFFPEAK_2026 = (0.04778 + 0.05251) / 2


# --- DB-backed lookup -------------------------------------------------------

# cost_map calls rate_for per hour x day, so the rows are loaded once and
# searched in memory. The cache is dropped on any save/delete of either
# model (billing/apps.py) and expires after CACHE_TTL_S, which covers
# writers in another process (the management commands).
CACHE_TTL_S = 60.0
_cache: tuple[float, dict, dict] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def _tables() -> tuple[dict, dict]:
    """(rates, adjustments): key -> ([effective_from...], [value...]), sorted."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL_S:
        return _cache[1], _cache[2]
    from .models import CcaAdjustment, UtilityRate

    rates: dict = {}
    for r in UtilityRate.objects.order_by("effective_from"):
        k = (r.season, r.period, r.tier)
        eff, vals = rates.setdefault(k, ([], []))
        eff.append(r.effective_from)
        vals.append(r.rate)
    adjs: dict = {}
    for a in CcaAdjustment.objects.order_by("effective_from"):
        eff, vals = adjs.setdefault((a.season, a.period), ([], []))
        eff.append(a.effective_from)
        vals.append(a.amount)
    _cache = (now, rates, adjs)
    return rates, adjs


def _latest(table: dict, key, d: date):
    """(effective_from, value) of the latest row for `key` effective on or before `d`."""
    if key not in table:
        return None
    eff, vals = table[key]
    i = bisect.bisect_right(eff, d)
    return (eff[i - 1], vals[i - 1]) if i else None


def is_peak(local_dt: datetime) -> bool:
    """4pm-9pm local, every day (E-TOU-C: peak pricing every day of the week)."""
    return PEAK_START_HOUR <= local_dt.hour < PEAK_END_HOUR


def is_summer(d: date) -> bool:
    """June through September."""
    return 6 <= d.month <= 9


def season_of(d: date) -> str:
    return "summer" if is_summer(d) else "winter"


def _tier_for(rates: dict, season: str, period: str, d: date) -> int:
    if season == "winter" and _latest(rates, (season, period, 2), d) is not None:
        return 2
    return 1


@dataclass(frozen=True)
class RateParts:
    pge: float  # UtilityRate, $/kWh
    adjustment: float  # CcaAdjustment, $/kWh (0.0 when none is in effect)
    tier: int

    @property
    def all_in(self) -> float:
        return self.pge + self.adjustment

    @property
    def adjustment_label(self) -> str:
        """'+ 0.082' / '− 0.014', for showing PG&E rate + adjustment."""
        return f"{'−' if self.adjustment < 0 else '+'} {abs(self.adjustment):.3f}"


def _parts(rates: dict, adjs: dict, season: str, period: str, d: date) -> RateParts | None:
    tier = _tier_for(rates, season, period, d)
    rate = _latest(rates, (season, period, tier), d)
    if rate is None:
        return None
    adj = _latest(adjs, (season, period), d)
    return RateParts(pge=rate[1], adjustment=adj[1] if adj else 0.0, tier=tier)


def parts_for(d: date, period: str) -> RateParts:
    """The PG&E rate and CCA adjustment behind `rates_for` for one period."""
    rates, adjs = _tables()
    parts = _parts(rates, adjs, season_of(d), period, d)
    if parts is None:
        raise LookupError(f"no {season_of(d)} {period} rate effective on {d}")
    return parts


def era_table(today: date | None = None) -> list[dict]:
    """One row per date any rate or adjustment changed, for the Grid page.

    Each row: effective date, RateParts (or None) per SEASON_PERIODS entry,
    the UtilityRate sources in play, and whether it is the era in effect
    `today`.
    """
    from .models import UtilityRate

    today = today or timezone.localdate()
    rates, adjs = _tables()
    dates = sorted({e for t in (rates, adjs) for eff, _ in t.values() for e in eff})
    sources = {
        (r.season, r.period, r.tier, r.effective_from): r.source for r in UtilityRate.objects.all()
    }
    eras = []
    for d in dates:
        cells = [_parts(rates, adjs, s, p, d) for s, p in SEASON_PERIODS]
        used = set()
        for (s, p), parts in zip(SEASON_PERIODS, cells):
            if parts:
                eff, _ = _latest(rates, (s, p, parts.tier), d)
                used.add(sources.get((s, p, parts.tier, eff), ""))
        eras.append({"effective": d, "cells": cells, "sources": sorted(used - {""})})
    current = max((e for e in eras if e["effective"] <= today), key=lambda e: e["effective"], default=None)
    for e in eras:
        e["current"] = e is current
    return eras


def rates_for(d: date) -> tuple[float, float]:
    """(peak $/kWh, off-peak $/kWh) all-in, in effect on `d`.

    The one place date->rate mapping lives, so per-period cost
    estimates (peak view) and per-instant lookups (rate_for) can't drift
    apart from each other.
    """
    return parts_for(d, "peak").all_in, parts_for(d, "offpeak").all_in


def rate_for(dt: datetime) -> float:
    """$/kWh for an aware datetime, converted to house-local time first."""
    local_dt = timezone.localtime(dt)
    peak_rate, offpeak_rate = rates_for(local_dt.date())
    return peak_rate if is_peak(local_dt) else offpeak_rate


def missing_adjustments(d: date | None = None) -> list[tuple[str, str]]:
    """(season, period) pairs priced on PG&E's side only as of `d`.

    A pair is missing when its latest UtilityRate is Opower-sourced (not
    an all-in seed) and the adjustment in effect predates the first such
    Opower row -- i.e. nobody has entered a WestLight/PCIA adjustment for
    the era the rate belongs to. Costs for those pairs omit the CCA side
    (usually a few cents/kWh, and it can be negative).
    """
    from .models import UtilityRate

    d = d or timezone.localdate()
    _, adjs = _tables()
    first_opower = {}
    for season, period, eff in (
        UtilityRate.objects.exclude(source="seed")
        .order_by("effective_from")
        .values_list("season", "period", "effective_from")
    ):
        first_opower.setdefault((season, period), eff)
    missing = []
    for season in SEASONS:
        for period in PERIODS:
            era_start = first_opower.get((season, period))
            if era_start is None or era_start > d:
                continue
            adj = _latest(adjs, (season, period), d)
            if adj is None or adj[0] < era_start:
                missing.append((season, period))
    return missing
