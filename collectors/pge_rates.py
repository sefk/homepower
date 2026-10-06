"""PG&E's own TOU rates, read back out of Opower's electric cost data.

Opower prices every hour of the ELEC account and splits each hour into
components by (season, day part, tier). cost / consumption of a component
is PG&E's net-usage rate less the baseline credit on tier 1 -- the
billing.UtilityRate basis. It excludes the CCA side (WestLight generation,
PG&E's generation credit, PCIA), which only a bill shows; that stays a
hand-entered billing.CcaAdjustment.

Each daily poll re-derives the trailing 30 days and records a new
UtilityRate whenever the rate moved, effective the first local day the new
rate appears. The derivation and change detection are pure functions of
the reads and the existing rows, so the same code backs the
`pge_rates_backfill` command over a longer window.

Same login as the gas collector (collectors/pge.py); same MFA caveat.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp
from django.db import transaction
from django.utils import timezone
from opower import AggregateType, MeterType, MfaChallenge, Opower

from billing import rates as billing_rates
from billing.models import UtilityRate
from core.models import Source

from .base import Collector, Reading
from .pge import REAUTH, UTILITY, load_login_data

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 30
# Components with less gross energy than this in a day carry rounded-to-
# the-cent cost, so their ratio is noise.
MIN_KWH = 1.0
# Smaller moves are rounding in Opower's cost, not a tariff change.
CHANGE_THRESHOLD = 0.0005
# Opower's hourly cost endpoint is requested a month at a time.
CHUNK_DAYS = 31

Key = tuple[str, str, int]  # (season, period, tier)


@dataclass(frozen=True)
class Change:
    key: Key
    effective_from: date
    rate: float
    previous: float | None
    update: bool = False  # revises the row already at effective_from


def _norm_season(season: str | None) -> str | None:
    return season.lower() if season else None


def _norm_period(day_part: str | None) -> str | None:
    """'ON_PEAK+RT02/TOD' -> 'peak'; 'OFF_PEAK' -> 'offpeak'."""
    if not day_part:
        return None
    part = day_part.split("+")[0].upper()
    return {"ON_PEAK": "peak", "OFF_PEAK": "offpeak"}.get(part)


def derive_daily_rates(reads) -> dict[date, dict[Key, float]]:
    """{local day: {(season, period, tier): $/kWh}} from hourly CostReads.

    Sums |cost| and |consumption| per component over the local day, then
    divides. Gross, not net: an import hour and an export hour carry the
    same rate with opposite signs, so net sums can cancel to near zero on
    a balanced day and leave per-hour rounding dominating the ratio.
    Components under MIN_KWH gross energy that day are skipped. Days with no readable component simply
    don't appear -- absence is unknown, never a rate of zero.
    """
    totals: dict[tuple[date, Key], list[float]] = defaultdict(lambda: [0.0, 0.0])  # [cost, kwh]
    for read in reads:
        day = timezone.localtime(read.start_time).date()
        for c in read.read_components:
            season, period = _norm_season(c.season), _norm_period(c.day_part)
            if season is None or period is None or c.tier_number is None:
                continue
            t = totals[(day, (season, period, int(c.tier_number)))]
            t[0] += abs(c.cost)
            t[1] += abs(c.consumption)
    daily: dict[date, dict[Key, float]] = defaultdict(dict)
    for (day, key), (cost, kwh) in totals.items():
        if kwh < MIN_KWH:
            continue
        daily[day][key] = cost / kwh
    return dict(daily)


def _per_key(daily: dict[date, dict[Key, float]], since: date) -> dict[Key, list[tuple[date, float]]]:
    series: dict[Key, list[tuple[date, float]]] = defaultdict(list)
    for day in sorted(daily):
        if day < since:
            continue
        for key, rate in daily[day].items():
            series[key].append((day, rate))
    return series


def rate_eras(daily, since: date = billing_rates.OPOWER_BASIS_START) -> dict[Key, list[tuple[date, date, float]]]:
    """Runs of unchanged rate per key: (first day seen, last day seen, rate).

    A run continues until the rate moves more than CHANGE_THRESHOLD from
    the run's first value. Independent of what's in the database.
    """
    eras: dict[Key, list[tuple[date, date, float]]] = {}
    for key, points in _per_key(daily, since).items():
        runs: list[list] = []
        for day, rate in points:
            if runs and abs(rate - runs[-1][2]) <= CHANGE_THRESHOLD:
                runs[-1][1] = day
            else:
                runs.append([day, day, rate])
        eras[key] = [tuple(r) for r in runs]
    return eras


def plan_changes(
    daily: dict[date, dict[Key, float]],
    existing: dict[Key, list[tuple[date, float, str]]],
    since: date = billing_rates.OPOWER_BASIS_START,
) -> list[Change]:
    """UtilityRate rows to write so the stored rates match `daily`.

    `existing` is {key: [(effective_from, rate, source)]}. Each observed
    day is compared with the row in effect on it (including rows planned
    earlier in this same pass); a difference over CHANGE_THRESHOLD opens a
    new row effective that day. If a row already sits on exactly that day
    it is revised, but only when its source is "opower" -- a bill-entered
    or seeded row is never overwritten. Days before `since` are ignored:
    earlier history is all-in seed data. Rerunning against the result is
    a no-op.
    """
    overlay = {k: sorted(v) for k, v in existing.items()}
    changes: list[Change] = []
    for key, points in _per_key(daily, since).items():
        rows = overlay.setdefault(key, [])
        protected_rate = None  # observed rate that a hand-entered row contradicts
        for day, rate in points:
            if protected_rate is not None:
                if abs(rate - protected_rate) <= CHANGE_THRESHOLD:
                    continue
                protected_rate = None
            current = None
            for eff, r, src in rows:
                if eff <= day:
                    current = (eff, r, src)
            if current is not None and abs(rate - current[1]) <= CHANGE_THRESHOLD:
                continue
            if current is not None and current[0] == day:
                if current[2] != "opower":
                    protected_rate = rate
                    continue
                rows[:] = [x for x in rows if x[0] != day] + [(day, rate, "opower")]
                changes.append(Change(key, day, rate, current[1], update=True))
            else:
                rows.append((day, rate, "opower"))
                rows.sort()
                changes.append(Change(key, day, rate, current[1] if current else None))
    return changes


def existing_rows() -> dict[Key, list[tuple[date, float, str]]]:
    rows: dict[Key, list[tuple[date, float, str]]] = defaultdict(list)
    for r in UtilityRate.objects.order_by("effective_from"):
        rows[(r.season, r.period, r.tier)].append((r.effective_from, r.rate, r.source))
    return dict(rows)


def apply_changes(changes: list[Change]) -> None:
    with transaction.atomic():
        for c in changes:
            season, period, tier = c.key
            UtilityRate.objects.update_or_create(
                season=season, period=period, tier=tier, effective_from=c.effective_from,
                defaults={"rate": c.rate, "source": "opower"},
            )


def describe(c: Change) -> str:
    season, period, tier = c.key
    was = f"{c.previous:.4f}" if c.previous is not None else "none"
    return f"{season} {period} tier {tier}: {was} -> {c.rate:.4f} from {c.effective_from}"


async def fetch_cost_reads(
    username: str, password: str, login_file: Path, start: datetime, end: datetime
) -> list:
    """Hourly ELEC CostReads over [start, end), requested a month at a time."""
    login_data = load_login_data(login_file)
    reads = []
    async with aiohttp.ClientSession() as session:
        opower = Opower(session, UTILITY, username, password, login_data=login_data)
        try:
            await opower.async_login()
        except MfaChallenge:
            raise RuntimeError(REAUTH) from None
        accounts = [a for a in await opower.async_get_accounts() if a.meter_type == MeterType.ELEC]
        if not accounts:
            raise RuntimeError("no electric account on this PG&E login")
        for account in accounts:
            chunk_start = start
            while chunk_start < end:
                chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end)
                reads += await opower.async_get_cost_reads(
                    account, AggregateType.HOUR, chunk_start, chunk_end
                )
                chunk_start = chunk_end
    return reads


class PgeRatesCollector(Collector):
    """Keeps billing.UtilityRate current. Writes no Samples: the output is
    rate rows, so /health/ judges it by its runs rather than by series."""

    slug = "pge_rates"
    name = "PG&E Rates"
    kind = Source.Kind.GRID
    poll_interval_s = 86400
    native_resolution_s = 86400

    def __init__(self, username: str, password: str, login_file: Path):
        super().__init__()
        self.username = username
        self.password = password
        self.login_file = login_file
        self._daily: dict = {}
        self._message = ""

    async def poll(self) -> list[Reading]:
        end = timezone.now()
        start = end - timedelta(days=LOOKBACK_DAYS)
        reads = await fetch_cost_reads(self.username, self.password, self.login_file, start, end)
        self._daily = derive_daily_rates(reads)
        return []

    def _store(self, readings: list[Reading]) -> None:
        changes = plan_changes(self._daily, existing_rows())
        apply_changes(changes)
        for c in changes:
            logger.warning("PG&E rate change detected: %s", describe(c))
        if changes:
            self._message = "RATE CHANGE: " + "; ".join(describe(c) for c in changes)
        else:
            self._message = f"no rate changes ({len(self._daily)} days read)"

    def _record_run(self, started, ok: bool, message: str) -> None:
        super()._record_run(started, ok, self._message if ok and self._message else message)
