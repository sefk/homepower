"""Hot tub load, estimated from step changes in the whole-home meter.

Pure functions, no database: they take sample lists (epoch seconds, watts)
and return what they found plus diagnostics. collectors/hottub_estimate.py
does the I/O.

The signature (see docs): the heater is a ~5.3 kW resistive step that
switches ON on the clock at 02:03:20-02:04:50 and 14:03:20-14:04:50 local
and runs 14-60 min; the pump draws ~0.5 kW for a fixed hour,
h:01:40 -> (h+1):01:40, with or without the heater. Nothing here is
metered -- a window the meter did not cover yields no estimate at all
(the caller leaves a gap), never a guess.
"""

import bisect
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WINDOW_HOURS = (2, 14)  # local clock hours the hot tub's timer fires
GATE_FROM_S = 2 * 60  # heater-on accepted h:02:00 .. h:08:00
GATE_TO_S = 8 * 60
WINDOW_END_S = 70 * 60  # a window spans h:00 .. (h+1):10
PUMP_ON_S = 100  # h:01:40
PUMP_LEN_S = 3600
PUMP_SEARCH = (40, 160)  # seconds after h:00 to look for the pump's rise
PUMP_BAND = (350.0, 1000.0)
PUMP_DEFAULT_W = 500.0

HEATER_BAND = {2: (4800.0, 5900.0), 14: (4300.0, 6200.0)}  # 2pm: noisier
MAX_STEP_W = 6200.0
OFF_AFTER_S = 5 * 60  # heater runs 5..60 min
OFF_BEFORE_S = 60 * 60
HOLD_S = 28 * 60  # median run, used when the fall is never seen
HOLD_CAP_S = 45 * 60
MAX_GAP_S = 150  # sample-to-sample gaps beyond this break an edge
TESLA_STEP_W = 500.0
TESLA_MARGIN_S = 60
TESLA_BRIDGE_S = 600  # a transition between samples this close is within the gap


@dataclass
class WindowResult:
    day: date
    hour: int
    start: float  # window span [start, end), epoch seconds
    end: float
    heater_on: float | None = None
    heater_off: float | None = None
    heater_w: float | None = None
    pump_w: float | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def pump_on(self) -> float:
        return self.start + PUMP_ON_S

    @property
    def pump_off(self) -> float:
        return self.pump_on + PUMP_LEN_S

    @property
    def heater_minutes(self) -> float:
        if self.heater_on is None:
            return 0.0
        return (self.heater_off - self.heater_on) / 60

    @property
    def kwh(self) -> float:
        wh = (self.pump_w or 0) * PUMP_LEN_S / 3600
        if self.heater_on is not None:
            wh += self.heater_w * (self.heater_off - self.heater_on) / 3600
        return wh / 1000

    def segments(self) -> list[tuple[float, float, float]]:
        """Piecewise-constant [a, b) -> watts covering the whole window span."""
        marks = {self.start, self.end, self.pump_on, self.pump_off}
        if self.heater_on is not None:
            marks |= {self.heater_on, self.heater_off}
        points = sorted(m for m in marks if self.start <= m <= self.end)
        out = []
        for a, b in zip(points, points[1:]):
            mid = (a + b) / 2
            w = self.pump_w if self.pump_on <= mid < self.pump_off else 0.0
            if self.heater_on is not None and self.heater_on <= mid < self.heater_off:
                w += self.heater_w
            out.append((a, b, w))
        return out


def local_epoch(day: date, hour: int, minute: int, second: int, tz: ZoneInfo) -> float:
    return datetime.combine(day, time(hour, minute, second), tz).timestamp()


def window_span(day: date, hour: int, tz: ZoneInfo) -> tuple[float, float]:
    start = local_epoch(day, hour, 0, 0, tz)
    return start, start + WINDOW_END_S


def _hold_lookup(series):
    """Value in force at t for a [(ts, w, duration_s)] list, or None when
    t is not within 2 durations of a sample (unknown, not zero)."""
    ts = [s[0] for s in series]

    def at(t):
        i = bisect.bisect_right(ts, t) - 1
        if i < 0 or t - ts[i] > 2 * series[i][2]:
            return None
        return series[i][1]

    return at


def house_load(eagle, solar_series, flags: list[str]):
    """[(ts, W)] of whole-home load: grid, plus solar production where
    the caller supplies it. Solar is held at its native resolution; where
    a solar source has no sample near, that source is left out and the
    window is flagged rather than counted as zero."""
    lookups = [_hold_lookup(s) for s in solar_series if s]
    if solar_series and not lookups:
        flags.append("no_solar")
    partial = False
    out = []
    for ts, w, _dur in eagle:
        total = w
        for look in lookups:
            v = look(ts)
            if v is None:
                partial = True
            else:
                total += v
        out.append((ts, total))
    if partial:
        flags.append("solar_partial")
    return out


def tesla_blackouts(tesla) -> list[tuple[float, float]]:
    """Intervals in which an EV charge start/stop may have hit the meter.
    tesla: [(ts, W, duration_s)] sorted. A transition seen between two
    close samples happened somewhere in that gap; between distant ones we
    only know it was seen at the later sample."""
    out = []
    for (t0, w0, _), (t1, w1, _) in zip(tesla, tesla[1:]):
        if abs(w1 - w0) < TESLA_STEP_W:
            continue
        a = t0 if t1 - t0 <= TESLA_BRIDGE_S else t1
        out.append((a - TESLA_MARGIN_S, t1 + TESLA_MARGIN_S))
    return out


def _edges(load, a, b, lo, hi, sign, blackouts):
    """Steps of sign*(mean of next 3 - mean of prev 3) in [lo, hi], with
    sample time in [a, b). Returns [(ts, step)], near-duplicates collapsed,
    plus the list of those suppressed by a Tesla transition."""
    ts = [p[0] for p in load]
    vs = [p[1] for p in load]
    i, j = bisect.bisect_left(ts, a), bisect.bisect_left(ts, b)
    found, suppressed = [], []
    for k in range(max(i, 3), min(j, len(vs) - 2)):
        if any(ts[m] - ts[m - 1] > MAX_GAP_S for m in range(k - 2, k + 3)):
            continue
        d = statistics.mean(vs[k : k + 3]) - statistics.mean(vs[k - 3 : k])
        if not (lo <= sign * d <= hi):
            continue
        if any(x <= ts[k] <= y for x, y in blackouts):
            suppressed.append(ts[k])
            continue
        if found and ts[k] - found[-1][0] < 40:
            if abs(d) > abs(found[-1][1]):
                found[-1] = (ts[k], d)
        else:
            found.append((ts[k], d))
    return found, suppressed


def _settled_step(load, t, before_s=60, after=(30, 90)):
    """Median load in [t+after[0], t+after[1]] less the median in
    [t-before_s, t), or None if either side has no samples."""
    pre = [w for ts, w in load if t - before_s <= ts < t]
    post = [w for ts, w in load if t + after[0] <= ts <= t + after[1]]
    if not pre or not post:
        return None
    return statistics.median(post) - statistics.median(pre)


def detect_window(day, hour, load, tesla_blackout_spans, tz: ZoneInfo) -> WindowResult:
    """Estimate one window from `load` ([(ts, W)] sorted, already the
    house load). The caller has checked the meter covers the window."""
    start, end = window_span(day, hour, tz)
    res = WindowResult(day=day, hour=hour, start=start, end=end)
    lo, hi = HEATER_BAND[hour]

    # Pump: its rise at h:01:40, measured once settled -- the motor's
    # ~1.2 kW inrush lasts ~30 s, which a 3-sample edge mean would count
    # (a 500 W pump read as ~870 W). Settled level is the median 30-90 s
    # after the rise (before the heater's earliest h:03:20 start) less the
    # median of the minute before. Else the nominal 500 W.
    pumps, _ = _edges(
        load, start + PUMP_SEARCH[0], start + PUMP_SEARCH[1], *PUMP_BAND, 1, tesla_blackout_spans
    )
    settled = _settled_step(load, pumps[-1][0]) if pumps else None
    if settled is not None and PUMP_BAND[0] <= settled <= PUMP_BAND[1]:
        res.pump_w = round(settled)
    else:
        res.pump_w = PUMP_DEFAULT_W
        res.flags.append("pump_assumed")

    ons, supp = _edges(
        load, start + GATE_FROM_S, start + GATE_TO_S, lo, hi, 1, tesla_blackout_spans
    )
    if supp:
        res.flags.append("tesla_ignored")
    if not ons:
        res.flags.append("no_heater")
        return res
    t_on, d_on = ons[0]
    offs, supp = _edges(
        load, t_on + OFF_AFTER_S, t_on + OFF_BEFORE_S, lo, hi, -1, tesla_blackout_spans
    )
    if supp:
        res.flags.append("tesla_ignored")
    res.heater_on = t_on
    if offs:
        t_off, d_off = offs[0]
        res.heater_off = t_off
        res.heater_w = round((d_on - d_off) / 2)
    else:
        res.heater_off = t_on + min(HOLD_S, HOLD_CAP_S)
        res.heater_w = round(d_on)
        res.flags.append("held")
    return res
