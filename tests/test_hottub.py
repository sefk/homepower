"""Hot Tub (estimated): step-change detection on synthetic Eagle traces,
the day writer's gap/coverage rules, the collector and the backfill command."""

from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command

from collectors import hottub_estimate
from collectors.hottub_detect import detect_window, house_load, tesla_blackouts
from collectors.hottub_estimate import HotTubEstimateCollector, derive_day
from core import coverage
from core.models import CollectorRun, CoverageSpan, Sample, Series, Source

LA = ZoneInfo("America/Los_Angeles")
UTC = dt_timezone.utc
DAY = date(2026, 10, 5)
BASE = 800.0  # background house load


def local(day, h, m=0, s=0):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=LA)


def trace(day, hour, heater=(None, None), heater_w=5300.0, pump_w=850.0, step=10, extra=None):
    """[(ts, W)] every `step` s over the window, h-0:05 .. h+1:15."""
    start = local(day, hour).timestamp()
    out = []
    for t in range(-300, 75 * 60, step):
        w = BASE
        if 100 <= t < 100 + 3600:
            w += pump_w
        if heater[0] is not None and heater[0] <= t < heater[1]:
            w += heater_w
        if extra:
            w += extra(t)
        out.append((start + t, w))
    return out


def detect(load, hour=2, day=DAY, blackouts=()):
    return detect_window(day, hour, load, list(blackouts), LA)


def at(r, ts):
    return datetime.fromtimestamp(ts, LA)


# ---------- pure detection ----------


def test_clean_2am_run():
    r = detect(trace(DAY, 2, heater=(224, 224 + 28 * 60)))
    assert abs(r.heater_on - (local(DAY, 2).timestamp() + 224)) <= 30
    assert r.heater_minutes == pytest.approx(28, abs=1)
    assert r.heater_w == pytest.approx(5300, abs=60)
    assert r.pump_w == pytest.approx(850, abs=60)
    assert "held" not in r.flags and "pump_assumed" not in r.flags
    # segments tile the window with 0 / pump / pump+heater
    segs = r.segments()
    assert segs[0][0] == r.start and segs[-1][1] == r.end
    assert all(a[1] == b[0] for a, b in zip(segs, segs[1:]))
    assert sorted({round(w, -2) for _, _, w in segs}) == [0, 800, 6200]


def test_2pm_run_with_solar_ramp():
    """Grid alone sees the heater fighting a solar ramp; grid + solar
    recovers it."""
    ramp = lambda t: -2.0 * t  # solar falling load off the grid, 2 W/s => -7 kW over the hour
    grid = trace(DAY, 14, heater=(224, 224 + 30 * 60), extra=ramp)
    start = local(DAY, 14).timestamp()
    # solar production = what the ramp removed from the grid, held per minute
    solar = [(start - 600 + 60 * i, 2.0 * ((start - 600 + 60 * i) - start) + 0.0, 60) for i in range(0, 90)]
    flags = []
    load = house_load([(t, w, 10) for t, w in grid], [solar], flags)
    r = detect(load, hour=14)
    assert r.heater_on is not None
    assert r.heater_minutes == pytest.approx(30, abs=1.5)
    assert "held" not in r.flags


def test_missing_fall_holds_median_and_flags():
    r = detect(trace(DAY, 2, heater=(224, 10_000)))  # never turns off inside the window
    assert r.heater_on is not None
    assert r.heater_minutes == pytest.approx(28, abs=0.1)
    assert "held" in r.flags


def test_pump_only_window():
    r = detect(trace(DAY, 2))
    assert r.heater_on is None and "no_heater" in r.flags
    assert r.pump_w == pytest.approx(850, abs=60)
    assert [round(w) for _, _, w in r.segments() if w] == [round(r.pump_w)]


def test_pump_inrush_is_not_counted():
    # Real trace (2026-10-08 02:01:38): +1180 W for ~16 s, +880 for ~16 s,
    # then a settled +500 W. The surge must not inflate the pump estimate.
    def inrush(t):
        return 680.0 if 100 <= t < 116 else 380.0 if 116 <= t < 132 else 0.0

    r = detect(trace(DAY, 2, pump_w=500.0, step=8, extra=inrush))
    assert r.pump_w == pytest.approx(500, abs=30)
    assert "pump_assumed" not in r.flags


def test_pump_assumed_when_step_unclear():
    r = detect(trace(DAY, 2, pump_w=0.0))
    assert r.pump_w == 500.0 and "pump_assumed" in r.flags


def test_heater_outside_gate_is_not_the_hot_tub():
    r = detect(trace(DAY, 2, heater=(15 * 60, 40 * 60)))  # on at h:15
    assert r.heater_on is None


def test_oversized_step_is_ignored():
    r = detect(trace(DAY, 2, heater=(224, 2000), heater_w=7000.0))
    assert r.heater_on is None


def test_tesla_overlap_ignored():
    start = local(DAY, 2).timestamp()
    tesla = [(start - 120, 0.0, 300), (start + 240, 7000.0, 300)]  # starts charging mid-window
    r = detect(trace(DAY, 2, heater=(224, 224 + 28 * 60)), blackouts=tesla_blackouts(tesla))
    assert r.heater_on is None
    assert "tesla_ignored" in r.flags


def test_tesla_far_from_edges_does_not_interfere():
    start = local(DAY, 2).timestamp()
    tesla = [(start - 7200, 0.0, 300), (start - 6900, 7000.0, 300)]
    r = detect(trace(DAY, 2, heater=(224, 224 + 28 * 60)), blackouts=tesla_blackouts(tesla))
    assert r.heater_on is not None


def test_gaps_in_eagle_break_edges():
    load = [p for p in trace(DAY, 2, heater=(224, 224 + 28 * 60)) if not (190 < p[0] - local(DAY, 2).timestamp() < 400)]
    assert detect(load).heater_on is None  # the rise falls inside a hole: unknown, not invented


def test_dst_day_uses_local_clock():
    """Nov 1 2026 falls back at 02:00 PDT -> 01:00 PST; the 2am window is
    02:00 PST = 10:00 UTC, not 09:00."""
    day = date(2026, 11, 1)
    r = detect(trace(day, 2, heater=(224, 224 + 28 * 60)), day=day)
    assert datetime.fromtimestamp(r.start, UTC).hour == 10
    assert r.heater_on is not None
    assert at(r, r.heater_on).hour == 2


# ---------- writer ----------


@pytest.fixture
def eagle(db):
    source = Source.objects.create(
        slug="eagle", name="Eagle", kind=Source.Kind.GRID, poll_interval_s=15, native_resolution_s=15
    )
    return Series.objects.create(source=source, metric="demand_w", unit="W")


def load_eagle(eagle, day, windows=(2, 14), heaters=None, covered=True):
    """Samples for each window (+ BASE elsewhere every 5 min), with live
    coverage over the whole local day unless `covered` is a (start, end)."""
    heaters = heaters or {}
    rows = []
    day0 = local(day, 0)
    for t in range(0, 24 * 3600, 300):
        ts = day0.astimezone(UTC) + timedelta(seconds=t)
        rows.append((ts.timestamp(), BASE))
    by_ts = dict(rows)
    for hour in windows:
        start = local(day, hour).timestamp()
        for t in range(-300, 75 * 60, 10):
            by_ts.pop(start + t, None)
        for ts, w in trace(day, hour, heater=heaters.get(hour, (None, None))):
            by_ts[ts] = w
    Sample.objects.bulk_create(
        Sample(series=eagle, ts=datetime.fromtimestamp(t, UTC), duration_s=10, value=w)
        for t, w in sorted(by_ts.items())
    )
    if covered is True:
        covered = (day0, day0 + timedelta(days=1))
    if covered:
        coverage.record_coverage(eagle, covered[0], covered[1], CoverageSpan.State.LIVE)


def out_series():
    return Series.objects.get(source__slug="hottub_est", metric="power_w")


def out_rows():
    return [(s.ts, s.duration_s, s.value) for s in Sample.objects.filter(series=out_series()).order_by("ts")]


def test_writes_contiguous_piecewise_day(eagle):
    load_eagle(eagle, DAY, heaters={2: (224, 224 + 1700), 14: (224, 224 + 1800)})
    r = derive_day(DAY, local(DAY + timedelta(days=1), 1))
    assert len(r.windows) == 2 and not r.skipped
    rows = out_rows()
    day0 = local(DAY, 0).astimezone(UTC)
    assert rows[0][0] == day0
    for (t0, d0, _), (t1, _, _) in zip(rows, rows[1:]):
        assert t0 + timedelta(seconds=d0) == t1  # no holes, no overlaps
    last_ts, last_d, _ = rows[-1]
    assert last_ts + timedelta(seconds=last_d) == local(DAY + timedelta(days=1), 0).astimezone(UTC)
    values = {round(v, -2) for _, _, v in rows}
    assert 0 in values and max(v for _, _, v in rows) > 5000
    assert coverage.covered_fraction(out_series(), day0, day0 + timedelta(days=1)) == pytest.approx(1.0)
    assert {s.state for s in coverage.spans(out_series(), day0, day0 + timedelta(days=1))} == {"backfilled"}


def test_no_eagle_coverage_yields_nothing(eagle):
    load_eagle(eagle, DAY, covered=False)
    r = derive_day(DAY, local(DAY + timedelta(days=1), 1))
    assert r.windows == [] and sorted(r.skipped) == [2, 14]
    assert out_rows() == []
    assert CoverageSpan.objects.filter(series=out_series()).count() == 0


def test_uncovered_window_stays_unknown_not_zero(eagle):
    """Eagle covered the afternoon only: the 2am window must be a hole in
    the output, and nothing is claimed before the Eagle's coverage."""
    day0 = local(DAY, 0)
    load_eagle(eagle, DAY, covered=(local(DAY, 12), day0 + timedelta(days=1)))
    r = derive_day(DAY, local(DAY + timedelta(days=1), 1))
    assert r.skipped == [2] and len(r.windows) == 1
    rows = out_rows()
    assert rows[0][0] == local(DAY, 12).astimezone(UTC)
    gaps = coverage.uncovered(out_series(), day0, day0 + timedelta(days=1))
    assert gaps == [(day0.astimezone(UTC), local(DAY, 12).astimezone(UTC))]


def test_hole_inside_window_leaves_gap(eagle):
    """Eagle coverage dips for 20 min inside the 2pm window: below the
    window threshold -> window unknown, with the rest of the day intact."""
    load_eagle(eagle, DAY, covered=(local(DAY, 0), local(DAY, 14, 10)))
    coverage.record_coverage(eagle, local(DAY, 14, 40), local(DAY, 23, 59), CoverageSpan.State.LIVE)
    r = derive_day(DAY, local(DAY + timedelta(days=1), 1))
    assert r.skipped == [14]
    gaps = coverage.uncovered(out_series(), local(DAY, 0), local(DAY, 23, 59))
    assert gaps and gaps[0][0] <= local(DAY, 14, 10).astimezone(UTC)


def test_idempotent_rerun(eagle):
    load_eagle(eagle, DAY, heaters={2: (224, 224 + 1700)})
    now = local(DAY + timedelta(days=1), 1)
    derive_day(DAY, now)
    first = out_rows()
    spans1 = [(s.start, s.end, s.state) for s in CoverageSpan.objects.filter(series=out_series())]
    derive_day(DAY, now)
    assert out_rows() == first
    assert [(s.start, s.end, s.state) for s in CoverageSpan.objects.filter(series=out_series())] == spans1


def test_partial_current_day_stops_at_last_finished_window(eagle):
    load_eagle(eagle, DAY, heaters={2: (224, 224 + 1700)})
    # 10:00 local: only the 2am window is finished
    r = derive_day(DAY, local(DAY, 10))
    assert [w.hour for w in r.windows] == [2]
    rows = out_rows()
    ts, dur, _ = rows[-1]
    assert ts + timedelta(seconds=dur) == local(DAY, 3, 10).astimezone(UTC)
    # 03:00 local: nothing finished yet
    Sample.objects.filter(series=out_series()).delete()
    assert derive_day(DAY, local(DAY, 3)).end is None
    assert out_rows() == []
    # 16:00: both finished, claimed through 15:10, not midnight
    derive_day(DAY, local(DAY, 16))
    ts, dur, _ = out_rows()[-1]
    assert ts + timedelta(seconds=dur) == local(DAY, 15, 10).astimezone(UTC)


def test_rewrite_replaces_stale_segments(eagle):
    load_eagle(eagle, DAY, heaters={2: (224, 224 + 1700)})
    derive_day(DAY, local(DAY + timedelta(days=1), 1))
    n = len(out_rows())
    # the heater evidence disappears (trace replaced): old heater segment must go
    Sample.objects.filter(series=eagle).delete()
    load_eagle(eagle, DAY, heaters={})
    CoverageSpan.objects.filter(series=eagle).delete()
    coverage.record_coverage(eagle, local(DAY, 0), local(DAY + timedelta(days=1), 0), CoverageSpan.State.LIVE)
    derive_day(DAY, local(DAY + timedelta(days=1), 1))
    assert max(v for _, _, v in out_rows()) < 2000
    assert len(out_rows()) < n


def test_dst_day_writes_25_hours(eagle):
    day = date(2026, 11, 1)
    load_eagle(eagle, day, heaters={2: (224, 224 + 1700)})
    coverage.record_coverage(eagle, local(day, 0), local(day + timedelta(days=1), 0), CoverageSpan.State.LIVE)
    r = derive_day(day, local(day + timedelta(days=1), 1))
    assert r.windows[0].heater_on is not None
    assert datetime.fromtimestamp(r.windows[0].start, UTC).hour == 10
    ts, dur, _ = out_rows()[-1]
    assert (ts + timedelta(seconds=dur) - local(day, 0).astimezone(UTC)) == timedelta(hours=25)


# ---------- collector and command ----------


def test_collector_registered_and_runs(eagle):
    from collectors.registry import enabled_collectors

    assert any(isinstance(c, HotTubEstimateCollector) for c in enabled_collectors())
    c = HotTubEstimateCollector()
    assert c.kind == Source.Kind.ESTIMATE and c.name == "Hot Tub (estimated)"
    c._source = c._ensure_source()
    c._store([])
    c._record_run(datetime.now(UTC), True, "x")
    run = CollectorRun.objects.get(source=c._source)
    assert run.ok and run.message


def test_collector_rederives_trailing_days(eagle, monkeypatch):
    today = date(2026, 10, 6)
    load_eagle(eagle, today - timedelta(days=1), heaters={2: (224, 224 + 1700)})
    load_eagle(eagle, today, heaters={2: (224, 224 + 1700)})
    now = local(today, 10)
    monkeypatch.setattr(hottub_estimate.timezone, "now", lambda: now)
    c = HotTubEstimateCollector()
    c._source = c._ensure_source()
    c._store([])
    c._store([])  # second pass: no duplicates
    rows = out_rows()
    assert len({r[0] for r in rows}) == len(rows)
    assert rows[0][0] == local(today - timedelta(days=1), 0).astimezone(UTC)
    ts, dur, _ = rows[-1]
    assert ts + timedelta(seconds=dur) == local(today, 3, 10).astimezone(UTC)


def test_backfill_command(eagle, monkeypatch):
    load_eagle(eagle, DAY, heaters={2: (224, 224 + 1700), 14: (224, 224 + 1800)})
    monkeypatch.setattr(hottub_estimate.timezone, "now", lambda: local(DAY + timedelta(days=1), 1))
    from collectors.management.commands import hottub_backfill

    monkeypatch.setattr(hottub_backfill.timezone, "now", lambda: local(DAY + timedelta(days=1), 1))
    out = StringIO()
    call_command("hottub_backfill", "--since", str(DAY), "--dry-run", stdout=out)
    assert "2 heater runs detected" in out.getvalue() and "dry run" in out.getvalue()
    assert not Sample.objects.filter(series__source__slug="hottub_est").exists()
    out = StringIO()
    call_command("hottub_backfill", "--since", str(DAY), stdout=out)
    assert out_rows()


def test_health_judges_estimate_by_its_runs(eagle, client):
    c = HotTubEstimateCollector()
    c._source = c._ensure_source()
    series = hottub_estimate.ensure_series()
    old = datetime.now(UTC) - timedelta(hours=10)
    Sample.objects.create(series=series, ts=old, duration_s=3600, value=0.0)
    CollectorRun.objects.create(source=c._source, started=datetime.now(UTC), finished=datetime.now(UTC), ok=True, message="m")
    resp = client.get("/health/")
    assert resp.status_code == 200
    assert b"Hot Tub (estimated)" in resp.content
    row = next(s for s in resp.context["sources"] if s["source"].slug == "hottub_est")
    assert not row["stale"]
