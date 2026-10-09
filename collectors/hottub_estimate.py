"""Hot Tub (estimated): a derived load, written from the Eagle's own samples.

Nothing is polled. Every ~15 minutes this re-derives the trailing local
days from the whole-home meter (collectors/hottub_detect.py) and rewrites
them: piecewise-constant Samples at 0 W / pump / pump+heater, so the
per-load charts show real zeros between runs. It is an estimate -- derived
from meter step changes at 2am/2pm, not metered -- and says so in its name.

Gap rules: samples exist only where the Eagle's coverage does, and a window
the Eagle did not cover (under WINDOW_COVERED of its span) is left unknown,
never written as zero. A day still in progress is written only through the
last window that has fully finished.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from core import coverage
from core.models import CoverageSpan, Sample, Series, Source

from .base import Collector, Reading
from .hottub_detect import (
    WINDOW_HOURS,
    WindowResult,
    detect_window,
    house_load,
    local_epoch,
    tesla_blackouts,
    window_span,
)

logger = logging.getLogger(__name__)

SLUG = "hottub_est"
NAME = "Hot Tub (estimated)"
TRAILING_DAYS = 2
WINDOW_COVERED = 0.9  # Eagle must cover this fraction of a window to estimate it
PAD_S = 300  # samples read either side of a window for the edge means


@dataclass
class DayResult:
    day: date
    end: datetime | None  # exclusive end of what was claimed; None = nothing yet
    windows: list[WindowResult] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)  # hours with no Eagle coverage
    samples: int = 0


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, dt_timezone.utc)


def day_bounds(day: date, now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    """(start, end) of what can be claimed for `day`, or None. A finished
    day is claimed whole; the current one only through its last finished
    window; a future day not at all."""
    start = datetime.combine(day, time(0), tz).astimezone(dt_timezone.utc)
    full_end = datetime.combine(day + timedelta(days=1), time(0), tz).astimezone(dt_timezone.utc)
    if now >= full_end:
        return start, full_end
    end = None
    for hour in WINDOW_HOURS:
        _, w_end = window_span(day, hour, tz)
        if now.timestamp() >= w_end:
            end = _utc(w_end)
    return (start, end) if end else None


def ensure_series() -> Series:
    collector = HotTubEstimateCollector()
    source = collector._ensure_source()
    series, _ = Series.objects.get_or_create(
        source=source, metric="power_w", defaults={"unit": "W"}
    )
    return series


def _samples(slug: str, metric: str, a: float, b: float) -> list[tuple[float, float, int]]:
    rows = Sample.objects.filter(
        series__source__slug=slug, series__metric=metric, ts__gte=_utc(a), ts__lt=_utc(b)
    ).order_by("ts").values_list("ts", "value", "duration_s")
    return [(t.timestamp(), v, d) for t, v, d in rows]


def _solar_before(a: float, b: float):
    # Solar rate of change is held at native resolution; start one quarter
    # hour early so the first Eagle samples of the window have a value.
    return [
        s
        for s in (
            _samples("envoy", "production_w", a - 1800, b),
            _samples("solaredge", "production_w", a - 1800, b),
        )
        if s
    ]


def derive_day(day: date, now: datetime, write: bool = True) -> DayResult:
    tz = ZoneInfo(timezone.get_current_timezone_name())
    bounds = day_bounds(day, now, tz)
    if bounds is None:
        return DayResult(day, None)
    start, end = bounds
    result = DayResult(day, end)

    eagle = Series.objects.filter(source__slug="eagle", metric="demand_w").first()
    if eagle is None:
        return DayResult(day, None)
    known = [
        (max(s.start, start), min(s.end, end)) for s in coverage.spans(eagle, start, end)
    ]

    claims: list[tuple[float, float, float]] = []
    unobserved: list[tuple[float, float]] = []
    for hour in WINDOW_HOURS:
        w_start, w_end = window_span(day, hour, tz)
        if w_end > end.timestamp():
            continue  # window not finished yet; not part of this claim
        if coverage.covered_fraction(eagle, _utc(w_start), _utc(w_end)) < WINDOW_COVERED:
            result.skipped.append(hour)
            unobserved.append((w_start, w_end))
            continue
        a, b = w_start - PAD_S, w_end + PAD_S
        flags: list[str] = []
        load = house_load(
            _samples("eagle", "demand_w", a, b),
            _solar_before(a, b) if hour == 14 else [],
            flags,
        )
        tesla = tesla_blackouts(_samples("tesla", "ev_charge_power_w", a - 3600, b + 3600))
        res = detect_window(day, hour, load, tesla, tz)
        res.flags[:0] = flags
        result.windows.append(res)
        claims += res.segments()

    slices = _stitch(known, unobserved, claims)
    result.samples = len(slices)
    if write:
        _write(start, end, slices)
    return result


def _stitch(known, unobserved, claims):
    """[(a, b, W)] epoch slices: only inside Eagle-known time, outside
    unobserved windows; claimed segments where there are any, else 0 W."""
    # Known time minus unobserved windows.
    pieces = []
    for k0, k1 in known:
        a, b = k0.timestamp(), k1.timestamp()
        cuts = [(max(a, u0), min(b, u1)) for u0, u1 in unobserved if u0 < b and u1 > a]
        cursor = a
        for c0, c1 in sorted(cuts):
            if c0 > cursor:
                pieces.append((cursor, c0))
            cursor = max(cursor, c1)
        if cursor < b:
            pieces.append((cursor, b))
    out = []
    for a, b in pieces:
        marks = {a, b}
        for c0, c1, _ in claims:
            marks |= {m for m in (c0, c1) if a < m < b}
        pts = sorted(marks)
        for x, y in zip(pts, pts[1:]):
            mid = (x + y) / 2
            w = next((cw for c0, c1, cw in claims if c0 <= mid < c1), 0.0)
            if out and out[-1][1] == x and out[-1][2] == w:
                out[-1] = (out[-1][0], y, w)
            else:
                out.append((x, y, w))
    return out


@transaction.atomic
def _write(start: datetime, end: datetime, slices) -> None:
    """Replace everything this estimate said about [start, end)."""
    series = ensure_series()
    Sample.objects.filter(series=series, ts__gte=start, ts__lt=end).delete()
    coverage.clear_coverage(series, start, end, CoverageSpan.State.BACKFILLED)
    Sample.objects.bulk_create(
        Sample(series=series, ts=_utc(a), duration_s=round(b - a), value=w)
        for a, b, w in slices
    )
    for a, b in _runs(slices):
        coverage.record_coverage(series, _utc(a), _utc(b), CoverageSpan.State.BACKFILLED)


def _runs(slices):
    runs = []
    for a, b, _ in slices:
        if runs and runs[-1][1] == a:
            runs[-1] = (runs[-1][0], b)
        else:
            runs.append((a, b))
    return runs


def derive_range(since: date, until: date, now: datetime, write: bool = True) -> list[DayResult]:
    return [
        derive_day(since + timedelta(days=i), now, write)
        for i in range((until - since).days + 1)
    ]


class HotTubEstimateCollector(Collector):
    """Reads the database only; poll() has nothing to fetch."""

    slug = SLUG
    name = NAME
    kind = Source.Kind.ESTIMATE
    poll_interval_s = 900
    native_resolution_s = 10  # the Eagle samples it is derived from

    async def poll(self) -> list[Reading]:
        return []

    def _store(self, readings: list[Reading]) -> None:
        now = timezone.now()
        today = timezone.localtime(now).date()
        results = derive_range(today - timedelta(days=TRAILING_DAYS - 1), today, now)
        self._message = "; ".join(
            f"{r.day}: {r.samples} samples, "
            + (
                ", ".join(
                    f"{w.hour:02d}h "
                    + (f"heater {w.heater_minutes:.0f} min" if w.heater_on else "no heater")
                    for w in r.windows
                )
                or "no window"
            )
            for r in results
        )

    def _record_run(self, started, ok: bool, message: str) -> None:
        super()._record_run(started, ok, getattr(self, "_message", "") if ok else message)
