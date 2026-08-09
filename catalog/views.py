"""Catalog views: the opinionated analyses (PRD: the catalog is the product).

The index page lists every analysis with the question it answers. Views
render gaps as gaps and cost views apply bill-derived TOU rates — nothing
here interpolates across a hole or treats missing samples as zero.
"""

import json
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Min
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from billing.cycles import cycle_label, true_up_cycles
from billing.models import BillPeriod, GasBillPeriod
from billing.rates import PEAK_END_HOUR, PEAK_START_HOUR, rate_for, rates_for
from core import coverage
from core.aggregate import energy_wh, grid_hourly_wh, has_grid_data
from core.models import CollectorRun, Sample, Series, Source

# A source is stale when it has been silent this many poll intervals.
STALE_POLLS = 3
HEALTH_WINDOW_DAYS = 7
PEAK_TABLE_DAYS = 7  # /peak/ live table: trailing days from Eagle data
COSTMAP_DAYS = 30  # /costmap/ heatmap: trailing days
# A heatmap cell can't show a coverage percentage the way the hourly
# tables do, so a partially-covered hour must be blank rather than show
# the cost of just the observed portion — otherwise an outage would
# quietly read as a cheap hour instead of a missing one. 0.99 (not 1.0)
# tolerates a collector's own rounding of interval boundaries.
COSTMAP_MIN_COVERAGE = 0.99

BASELINE_DAYS = 90  # /baseline/ trailing window (PRD: trended over months)
# Below this, a day's 3-5am window is unknown, not a fake minimum.
BASELINE_MIN_COVERAGE = 0.9
SELFUSE_DAYS = 30  # /selfuse/ trailing window
# Both generation and grid coverage must clear this for a day to render.
SELFUSE_MIN_COVERAGE = 0.9
SOLARHEALTH_DAYS = 7  # /solarhealth/ trailing window

# kWh of thermal energy per therm (EIA constant); heat-pump electric kWh =
# therms * THERM_TO_KWH_THERMAL / COP.
THERM_TO_KWH_THERMAL = 29.3
ELECTRIFY_DEFAULT_COP = 3.0
ELECTRIFY_COP_MIN = 1.5
ELECTRIFY_COP_MAX = 5.0
# Rough annual production per installed kW at this latitude (NREL
# PVWatts-ish figure for the Bay Area) -- used only for the /electrify/
# array-shortfall estimate, not for any per-instant modeling.
ARRAY_KWH_PER_KW_YEAR = 1450
# Trailing bill periods that make up the "annual" heat-pump total -- the
# most recent 12 GasBillPeriod rows by end_date, one per month.
ANNUAL_GAS_PERIODS = 12

# Analysis catalog for the index page (PRD: the catalog is the product).
# Each entry states the one-sentence question it answers, not a chart type.
CATALOG_GROUPS = [
    {
        "name": "Cost & TOU",
        "entries": [
            {
                "title": "True-up tracker",
                "url_name": "catalog:trueup",
                "question": "Where is this true-up cycle heading, in kWh and dollars, against last year's?",
            },
            {
                "title": "Peak-window bill share",
                "url_name": "catalog:peak",
                "question": "What share of the bill is the 4–9pm window?",
            },
            {
                "title": "Cost heatmap",
                "url_name": "catalog:costmap",
                "question": "Which hours cost the money?",
            },
            {
                "title": "Electrification modeling",
                "url_name": "catalog:electrify",
                "question": "What would winter gas load cost as heat-pump electric, and how big a shortfall would the array have?",
            },
        ],
    },
    {
        "name": "Grid",
        "entries": [
            {
                "title": "Grid demand",
                "url_name": "catalog:grid",
                "question": "When is the house importing from or exporting to the grid, and by how much?",
            },
        ],
    },
    {
        "name": "Baseline",
        "entries": [
            {
                "title": "Overnight floor",
                "url_name": "catalog:baseline",
                "question": "Is the 3–5am baseline load rising — is something new always-on?",
            },
        ],
    },
    {
        "name": "Solar",
        "entries": [
            {
                "title": "Solar production",
                "url_name": "catalog:solar",
                "question": "How much are the arrays producing right now, and where's the data missing?",
            },
            {
                "title": "Self-consumption",
                "url_name": "catalog:selfuse",
                "question": "How much generation is used on-site vs. exported, and does export timing miss the 4–9pm window?",
            },
            {
                "title": "Solar health",
                "url_name": "catalog:solarhealth",
                "question": "Are the arrays performing alike, per kW installed?",
            },
        ],
    },
    {
        "name": "Data health",
        "entries": [
            {
                "title": "Data health",
                "url_name": "catalog:health",
                "question": "Per source: what's been collected, backfilled, or never attempted?",
            },
        ],
    },
]

# Analyses the PRD calls for that can't ship yet — no source data, no
# collector, or not enough history. Shown dimmed so the catalog stays
# honest about what it doesn't do yet, rather than looking unfinished.
WAITING_GROUP = {
    "name": "Waiting on data or source",
    "entries": [
        {
            "title": "Peak decomposition",
            "question": "Stacked 4–9pm attribution: EV / hot tub / baseline / unexplained.",
            "why": "the Eagle 3 is live, but this needs load-identification models plus more accumulated history",
        },
        {
            "title": "Load signatures",
            "question": "Isolate the hot tub, dryer, and EV charging by their on/off shape.",
            "why": "the Eagle 3 is live, but this needs load-identification models plus more accumulated history",
        },
        {
            "title": "Clear-sky ratio",
            "question": "How does actual output compare to modeled expected output?",
            "why": "needs a clear-sky production model, not yet built",
        },
        {
            "title": "Degradation trend",
            "question": "Is the 2015 SunPower array's annual peak output declining?",
            "why": "needs multiple years of production history",
        },
        {
            "title": "EV charge sessions",
            "question": "What did each charge session cost at the TOU rate in effect?",
            "why": "needs the Tesla Fleet API collector, not yet built",
        },
        {
            "title": "EV counterfactual",
            "question": "What would those sessions have cost shifted past 9pm?",
            "why": "needs the Tesla Fleet API collector, not yet built",
        },
    ],
}


def index(request):
    """The analysis catalog — every view, grouped, with the question it answers."""
    return render(
        request,
        "catalog/index.html",
        {"groups": CATALOG_GROUPS, "waiting": WAITING_GROUP},
    )


def health(request):
    """Per-source coverage timeline, freshness, and recent failures."""
    now = timezone.now()
    window_start = now - timedelta(days=HEALTH_WINDOW_DAYS)
    window_s = (now - window_start).total_seconds()

    sources = []
    for source in Source.objects.order_by("slug"):
        series_rows = []
        for series in source.series.order_by("metric"):
            spans = []
            for span in coverage.spans(series, window_start, now):
                left = max((span.start - window_start).total_seconds(), 0)
                right = min((span.end - window_start).total_seconds(), window_s)
                spans.append(
                    {
                        "state": span.state,
                        "left_pct": 100 * left / window_s,
                        "width_pct": 100 * (right - left) / window_s,
                    }
                )
            covered = coverage.covered_fraction(series, window_start, now)
            series_rows.append(
                {"series": series, "spans": spans, "covered_pct": 100 * covered}
            )

        last_sample = (
            Sample.objects.filter(series__source=source).order_by("-ts").first()
        )
        stale_after = timedelta(seconds=STALE_POLLS * source.poll_interval_s)
        sources.append(
            {
                "source": source,
                "push": source.slug == "eagle",  # push sources have no poll loop
                "series_rows": series_rows,
                "last_sample": last_sample,
                "stale": last_sample is None or now - last_sample.ts > stale_after,
                "failures": list(
                    source.runs.filter(ok=False).order_by("-started")[:5]
                ),
            }
        )

    return render(
        request,
        "catalog/health.html",
        {
            "section": "health",
            "sources": sources,
            "window_start": window_start,
            "window_days": HEALTH_WINDOW_DAYS,
            "now": now,
        },
    )


def _window(request):
    """Resolve ?date= (local) and ?range=day|week to an aware UTC window."""
    tz = timezone.get_current_timezone()
    date_param = request.GET.get("date")
    if date_param:
        try:
            day = datetime.strptime(date_param, "%Y-%m-%d").date()
        except ValueError:
            raise Http404("bad date")
    else:
        day = timezone.localdate()
    span = request.GET.get("range", "day")
    days = 7 if span == "week" else 1
    end_day = day + timedelta(days=1)
    start = datetime.combine(end_day - timedelta(days=days), time.min, tzinfo=tz)
    end = datetime.combine(end_day, time.min, tzinfo=tz)
    return day, span, days, start, end


def solar(request):
    """Solar production, all arrays; hourly table twin per source."""
    day, span, days, start, end = _window(request)

    # One hourly table per array — with two solar sources an unscoped
    # .first() would nondeterministically label one array as the other.
    tables = []
    if days == 1:
        production = Series.objects.filter(
            source__kind=Source.Kind.SOLAR, metric="production_w"
        ).select_related("source").order_by("source__slug")
        for series in production:
            hours = []
            for h in range(24):
                h_start = start + timedelta(hours=h)
                result = energy_wh(series, h_start, h_start + timedelta(hours=1))
                hours.append(
                    {
                        "hour": h_start,
                        "wh": result.wh,
                        "coverage_pct": 100 * result.coverage,
                    }
                )
            tables.append({"source": series.source, "hours": hours})

    return render(
        request,
        "catalog/solar.html",
        {
            "section": "solar",
            "day": day,
            "range": span,
            "prev_day": day - timedelta(days=days),
            "next_day": day + timedelta(days=days),
            "today": timezone.localdate(),
            "tables": tables,
        },
    )


def _traces(series_qs, start, end):
    """Plotly traces. Lines break wherever coverage says unknown.

    Coverage is the single authority on gaps — no per-chart timestamp
    thresholds to drift out of sync with each collector's grace rules.
    (A confirmed_empty span with no samples would still draw across; no
    source writes that state yet.)
    """
    tz = timezone.get_current_timezone()
    traces = []
    for series in series_qs.select_related("source"):
        resolution = series.source.native_resolution_s
        gaps = coverage.uncovered(series, start, end)
        xs, ys = [], []
        prev_ts = None
        gi = 0
        samples = Sample.objects.filter(
            series=series, ts__gte=start, ts__lt=end
        ).order_by("ts")
        for s in samples.iterator():
            if prev_ts is not None:
                while gi < len(gaps) and gaps[gi][1] <= prev_ts:
                    gi += 1
                if gi < len(gaps) and gaps[gi][0] < s.ts and gaps[gi][1] > prev_ts:
                    xs.append(None)  # unknown stretch: break the line
                    ys.append(None)
            xs.append(s.ts.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"))
            ys.append(s.value)
            prev_ts = s.ts
        traces.append(
            {
                "name": f"{series.source.name} — {series.metric}",
                "metric": series.metric,
                "x": xs,
                "y": ys,
                # coarse sources render stepped, not smoothed (PRD invariant)
                "stepped": resolution > 60,
                "resolution_s": resolution,
            }
        )
    return traces


def solar_data(request):
    _, _, _, start, end = _window(request)
    return JsonResponse(
        {"traces": _traces(Series.objects.filter(source__kind=Source.Kind.SOLAR), start, end)}
    )


def grid(request):
    """Whole-home grid demand; hourly import/export table twin rides along.

    The table uses grid_hourly_wh (demand_w, or the Green Button backfill
    when that's all there is); the chart below stays demand_w-only via
    grid_data — a line chart of interval energy samples doesn't mean
    anything, so Green Button data never appears there.
    """
    day, span, days, start, end = _window(request)

    hours = []
    if has_grid_data() and days == 1:
        for h in range(24):
            h_start = start + timedelta(hours=h)
            result = grid_hourly_wh(h_start, h_start + timedelta(hours=1))
            hours.append(
                {
                    "hour": h_start,
                    "imported_wh": result.imported_wh,
                    "exported_wh": result.exported_wh,
                    "coverage_pct": 100 * result.coverage,
                }
            )

    return render(
        request,
        "catalog/grid.html",
        {
            "section": "grid",
            "day": day,
            "range": span,
            "prev_day": day - timedelta(days=days),
            "next_day": day + timedelta(days=days),
            "today": timezone.localdate(),
            "hours": hours,
        },
    )


def grid_data(request):
    _, _, _, start, end = _window(request)
    series_qs = Series.objects.filter(source__kind=Source.Kind.GRID, metric="demand_w")
    return JsonResponse({"traces": _traces(series_qs, start, end)})


def trueup(request):
    """Where is this true-up cycle heading? Cumulative net kWh / $ per
    April-to-April cycle, purely from billed history — no live projection."""
    return render(request, "catalog/trueup.html", {"section": "catalog"})


def trueup_data(request):
    periods = list(BillPeriod.objects.order_by("end_date"))
    cycles = true_up_cycles(periods)

    traces = []
    outcome = None
    for cycle in cycles:
        # A cycle is complete when it closed with an April bill, not merely
        # because a later cycle exists — if the dataset's newest bill IS
        # that April closer, this is still the last entry in `cycles` and
        # must still report its outcome. Both boundaries must hold: a
        # dataset truncated mid-cycle (say Jan..April) ends in April but
        # is a partial sum, not a closed true-up outcome.
        complete = (
            cycle[-1].end_date.month == 4 and cycle[0].end_date.month == 5
        )
        cum_kwh, cum_dollars = [], []
        running_kwh = running_dollars = 0.0
        for period in cycle:
            running_kwh += period.net_kwh
            running_dollars += period.nem_charges
            cum_kwh.append(running_kwh)
            cum_dollars.append(running_dollars)
        traces.append(
            {
                "label": cycle_label(cycle),
                "months": list(range(1, len(cycle) + 1)),
                "net_kwh": cum_kwh,
                "nem_charges": cum_dollars,
                "complete": complete,
            }
        )
        if complete:
            outcome = {
                "label": cycle_label(cycle),
                "net_kwh": cum_kwh[-1],
                "nem_charges": cum_dollars[-1],
            }

    return JsonResponse({"cycles": traces, "outcome": outcome})


def _peak_offpeak_wh(local_day, tz):
    """Imported Wh and coverage for a local day, split at the 4-9pm window.

    Off-peak is two sub-windows (midnight-4pm, 9pm-midnight); their imported
    Wh sums and their coverage combines as a duration-weighted average — the
    same arithmetic covered_fraction does over a single window, just applied
    across the two pieces (never our own gap detection — coverage stays the
    one authority, per docs/REVIEW-INSIGHTS.md).

    Weights come from UTC-normalized boundaries, not wall-clock subtraction:
    two datetimes sharing the same tzinfo object subtract by wall clock in
    Python, so on a DST transition day dur_morning/dur_evening would be off
    by an hour (docs/REVIEW-INSIGHTS.md "DST is a standing adversary").

    Uses grid_hourly_wh, not a specific Series, so a Green Button-only
    period (no Eagle 3 data at all) still populates this table.
    """
    day_start = datetime.combine(local_day, time.min, tzinfo=tz)
    peak_start = datetime.combine(local_day, time(hour=PEAK_START_HOUR), tzinfo=tz)
    peak_end = datetime.combine(local_day, time(hour=PEAK_END_HOUR), tzinfo=tz)
    next_day_start = day_start + timedelta(days=1)

    peak = grid_hourly_wh(peak_start, peak_end)
    off_morning = grid_hourly_wh(day_start, peak_start)
    off_evening = grid_hourly_wh(peak_end, next_day_start)

    dur_morning = (coverage._utc(peak_start) - coverage._utc(day_start)).total_seconds()
    dur_evening = (coverage._utc(next_day_start) - coverage._utc(peak_end)).total_seconds()
    dur_off = dur_morning + dur_evening
    offpeak_wh = off_morning.imported_wh + off_evening.imported_wh
    offpeak_coverage = (
        off_morning.coverage * dur_morning + off_evening.coverage * dur_evening
    ) / dur_off

    return peak.imported_wh, peak.coverage, offpeak_wh, offpeak_coverage


def peak(request):
    """What share of the bill is the 4-9pm window? Billed peak/off-peak $
    per period, plus a live 7-day table of the same split from Eagle data.

    Off-peak spans two sub-windows (midnight-4pm, 9pm-midnight) that never
    cross a season boundary — same local day, so one off-peak rate covers
    both.
    """
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    days = [today - timedelta(days=i) for i in range(PEAK_TABLE_DAYS - 1, -1, -1)]

    rows = []
    if has_grid_data():
        for day in days:
            peak_wh, peak_cov, offpeak_wh, offpeak_cov = _peak_offpeak_wh(day, tz)
            peak_rate, offpeak_rate = rates_for(day)
            rows.append(
                {
                    "day": day,
                    "peak_dollars": peak_wh / 1000 * peak_rate,
                    "peak_coverage_pct": 100 * peak_cov,
                    "offpeak_dollars": offpeak_wh / 1000 * offpeak_rate,
                    "offpeak_coverage_pct": 100 * offpeak_cov,
                }
            )

    return render(request, "catalog/peak.html", {"section": "catalog", "rows": rows})


def peak_data(request):
    """Billed peak/off-peak kWh per period, plus the estimated $ split at
    the bill-derived rate for the period's season (PRD: what the 4-9pm
    window COSTS, not just how many kWh it is — equal kWh isn't equal
    dollars under TOU)."""
    periods = BillPeriod.objects.order_by("end_date")
    labels, peak_kwh, offpeak_kwh, peak_dollars, offpeak_dollars = [], [], [], [], []
    for p in periods:
        peak_rate, offpeak_rate = rates_for(p.end_date)
        labels.append(p.end_date.isoformat())
        peak_kwh.append(p.peak_kwh)
        offpeak_kwh.append(p.offpeak_kwh)
        peak_dollars.append(p.peak_kwh * peak_rate)
        offpeak_dollars.append(p.offpeak_kwh * offpeak_rate)
    return JsonResponse(
        {
            "labels": labels,
            "peak_kwh": peak_kwh,
            "offpeak_kwh": offpeak_kwh,
            "peak_dollars": peak_dollars,
            "offpeak_dollars": offpeak_dollars,
        }
    )


def costmap(request):
    """Which hours cost the money? Hour x day heatmap of import cost,
    bill-derived rates applied; uncovered or partially-covered hours are
    blank, never zero."""
    return render(request, "catalog/costmap.html", {"section": "catalog"})


def costmap_data(request):
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    days = [today - timedelta(days=i) for i in range(COSTMAP_DAYS - 1, -1, -1)]

    z = [[None] * len(days) for _ in range(24)]
    if has_grid_data():
        for di, day in enumerate(days):
            day_start = datetime.combine(day, time.min, tzinfo=tz)
            for h in range(24):
                h_start = day_start + timedelta(hours=h)
                result = grid_hourly_wh(h_start, h_start + timedelta(hours=1))
                # partial coverage stays null too — see COSTMAP_MIN_COVERAGE
                if result.coverage >= COSTMAP_MIN_COVERAGE:
                    z[h][di] = result.imported_wh / 1000 * rate_for(h_start)

    return JsonResponse(
        {"days": [d.isoformat() for d in days], "hours": list(range(24)), "z": z}
    )


def _baseline_days():
    """Trailing BASELINE_DAYS local days: the 3-5am minimum demand_w, but
    only for days whose 3-5am coverage clears BASELINE_MIN_COVERAGE. A day
    with a hole in that window is None -- a gap in the chart, never a
    fake minimum (PRD: absence of a sample means unknown, never zero)."""
    tz = timezone.get_current_timezone()
    series = Series.objects.filter(source__kind=Source.Kind.GRID, metric="demand_w").first()
    today = timezone.localdate()
    days = [today - timedelta(days=i) for i in range(BASELINE_DAYS - 1, -1, -1)]

    labels, mins = [], []
    qualifying = 0
    if series:
        for day in days:
            start = datetime.combine(day, time(hour=3), tzinfo=tz)
            end = datetime.combine(day, time(hour=5), tzinfo=tz)
            labels.append(day.isoformat())
            if coverage.covered_fraction(series, start, end) >= BASELINE_MIN_COVERAGE:
                min_w = Sample.objects.filter(
                    series=series, ts__gte=start, ts__lt=end
                ).aggregate(Min("value"))["value__min"]
                mins.append(min_w)
                qualifying += 1
            else:
                mins.append(None)
    return labels, mins, qualifying


def baseline(request):
    """Is the overnight floor rising -- is something new always-on? 3-5am
    minimum demand, trended over the trailing 90 days."""
    _, _, qualifying = _baseline_days()
    return render(
        request,
        "catalog/baseline.html",
        {"section": "catalog", "has_data": qualifying > 0, "window_days": BASELINE_DAYS},
    )


def baseline_data(request):
    labels, mins, qualifying = _baseline_days()
    return JsonResponse({"days": labels, "min_w": mins, "qualifying_days": qualifying})


def _electrify_cop(request):
    """?cop= clamped to [ELECTRIFY_COP_MIN, ELECTRIFY_COP_MAX]. A missing
    or unparseable value falls back to the default rather than 404ing --
    this is a modeling knob, not a navigable resource."""
    try:
        cop = float(request.GET.get("cop", ELECTRIFY_DEFAULT_COP))
    except ValueError:
        cop = ELECTRIFY_DEFAULT_COP
    return max(ELECTRIFY_COP_MIN, min(ELECTRIFY_COP_MAX, cop))


def electrify(request):
    """What would winter gas heat cost as electric, and can the array
    carry it? Modeled entirely from seeded gas/electric bills -- no live
    data needed."""
    return render(
        request, "catalog/electrify.html", {"section": "catalog", "cop": _electrify_cop(request)}
    )


def electrify_data(request):
    cop = _electrify_cop(request)
    bills_by_month = {
        (p.end_date.year, p.end_date.month): p for p in BillPeriod.objects.all()
    }
    gas_periods = list(GasBillPeriod.objects.order_by("end_date"))
    # "Annual" heat-pump load is the trailing ANNUAL_GAS_PERIODS bill
    # periods by end_date, not the whole table -- summing every seeded gas
    # bill ever would inflate the "annual" figure (and the shortfall stat
    # derived from it) as more years of history accumulate.
    annual_ids = {g.id for g in gas_periods[-ANNUAL_GAS_PERIODS:]}

    labels, therms, heat_pump_kwh, net_kwh = [], [], [], []
    annual_kwh = 0.0
    for g in gas_periods:
        kwh = g.therms * THERM_TO_KWH_THERMAL / cop
        labels.append(g.end_date.isoformat())
        therms.append(g.therms)
        heat_pump_kwh.append(kwh)
        if g.id in annual_ids:
            annual_kwh += kwh
        # Gas and electric bill cycles don't share boundaries; matched by
        # end-month as an approximation (template footer notes this).
        match = bills_by_month.get((g.end_date.year, g.end_date.month))
        net_kwh.append(match.net_kwh if match else None)

    annual_periods = gas_periods[-ANNUAL_GAS_PERIODS:]
    return JsonResponse(
        {
            "labels": labels,
            "therms": therms,
            "heat_pump_kwh": heat_pump_kwh,
            "net_kwh": net_kwh,
            "cop": cop,
            "annual_heat_pump_kwh": annual_kwh,
            "shortfall_kw": annual_kwh / ARRAY_KWH_PER_KW_YEAR,
            "annual_window": {
                "start": annual_periods[0].end_date.isoformat() if annual_periods else None,
                "end": annual_periods[-1].end_date.isoformat() if annual_periods else None,
                "periods": len(annual_periods),
            },
        }
    )


def _selfuse_day(day, tz, solar_series, has_grid):
    """generation/export/self-use for one local day, or None if either
    side's coverage misses SELFUSE_MIN_COVERAGE -- never a partial number
    passed off as the whole day."""
    day_start = datetime.combine(day, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    generation_wh = 0.0
    gen_coverage = 0.0
    if solar_series:
        gen_coverage = 1.0
        for series in solar_series:
            result = energy_wh(series, day_start, day_end)
            generation_wh += result.wh
            # Weakest-covered array sets the day's generation coverage --
            # a good ADU day can't paper over a bad SolarEdge day.
            gen_coverage = min(gen_coverage, result.coverage)

    grid_coverage = 0.0
    exported_wh = 0.0
    if has_grid:
        # grid_hourly_wh, not a specific Series: a Green Button-only
        # period still has an export figure to compare against generation.
        split = grid_hourly_wh(day_start, day_end)
        exported_wh = split.exported_wh
        grid_coverage = split.coverage

    if gen_coverage < SELFUSE_MIN_COVERAGE or grid_coverage < SELFUSE_MIN_COVERAGE:
        return None

    if exported_wh > generation_wh:
        # Both sides cleared the coverage floor but are still partial, and
        # measured exports exceed measured generation -- physically that
        # can only mean generation samples are missing, not that the
        # house exported more than it made. That's indistinguishable from
        # "not enough generation coverage," so it's a gap, not a clamped
        # self-used of 0 (which would silently render unknown as zero).
        return None

    self_used_wh = max(generation_wh - exported_wh, 0.0)
    fraction = self_used_wh / generation_wh if generation_wh > 0 else None
    return {
        "generation_wh": generation_wh,
        "exported_wh": exported_wh,
        "self_used_wh": self_used_wh,
        "fraction": fraction,
    }


def _most_recent_covered_grid_day(tz, grid_series, lookback_days=SELFUSE_DAYS):
    """Most recent local day with near-complete grid coverage, for the
    companion export-timing chart -- None if nothing qualifies yet."""
    if not grid_series:
        return None
    today = timezone.localdate()
    for i in range(lookback_days):
        day = today - timedelta(days=i)
        day_start = datetime.combine(day, time.min, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        if coverage.covered_fraction(grid_series, day_start, day_end) >= SELFUSE_MIN_COVERAGE:
            return day
    return None


def selfuse(request):
    """How much generation is used on-site vs. exported? Stacked
    self-used/exported per day over the trailing month, plus a companion
    hourly chart shading the 4-9pm window for the most recent
    fully-covered day (export timing vs. peak windows, PRD)."""
    tz = timezone.get_current_timezone()
    grid_series = Series.objects.filter(source__kind=Source.Kind.GRID, metric="demand_w").first()
    return render(
        request,
        "catalog/selfuse.html",
        {
            "section": "catalog",
            # PRD's main array is SolarEdge; until that source exists the
            # ADU (Envoy) is the whole picture.
            "solaredge_live": Source.objects.filter(slug="solaredge").exists(),
            "recent_day": _most_recent_covered_grid_day(tz, grid_series),
        },
    )


def selfuse_data(request):
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    days = [today - timedelta(days=i) for i in range(SELFUSE_DAYS - 1, -1, -1)]
    solar_series = list(
        Series.objects.filter(source__kind=Source.Kind.SOLAR, metric="production_w")
    )
    has_grid = has_grid_data()

    labels, self_used, exported, fractions = [], [], [], []
    for day in days:
        labels.append(day.isoformat())
        result = _selfuse_day(day, tz, solar_series, has_grid)
        self_used.append(result["self_used_wh"] if result else None)
        exported.append(result["exported_wh"] if result else None)
        fractions.append(result["fraction"] if result else None)

    return JsonResponse(
        {"days": labels, "self_used_wh": self_used, "exported_wh": exported, "fraction": fractions}
    )


# Installed capacity per source slug, for /solarhealth/'s W/kW
# normalization. Only sources with a known figure here get scaled.
_SOLAR_KW_BY_SLUG = {"envoy": settings.SOLAR_ADU_KW, "solaredge": settings.SOLAR_MAIN_KW}


def solarhealth(request):
    """Are the arrays performing alike, per kW installed? Two arrays, two
    vendors, one axis -- divergence is the signal (PRD "Solar health")."""
    return render(
        request,
        "catalog/solarhealth.html",
        {
            "section": "catalog",
            "comparison_active": Source.objects.filter(slug="solaredge").exists(),
            "adu_kw": settings.SOLAR_ADU_KW,
            "main_kw": settings.SOLAR_MAIN_KW,
        },
    )


def solarhealth_data(request):
    tz = timezone.get_current_timezone()
    end = datetime.combine(timezone.localdate() + timedelta(days=1), time.min, tzinfo=tz)
    start = end - timedelta(days=SOLARHEALTH_DAYS)

    # Ordered explicitly so this queryset's iteration order matches the
    # one _traces uses internally -- the zip below relies on that.
    series_qs = Series.objects.filter(
        source__kind=Source.Kind.SOLAR, metric="production_w"
    ).order_by("source__slug")
    traces = _traces(series_qs, start, end)
    for t, series in zip(traces, series_qs.select_related("source")):
        kw = _SOLAR_KW_BY_SLUG.get(series.source.slug)
        if kw:
            t["y"] = [v / kw if v is not None else None for v in t["y"]]
            t["name"] = f"{series.source.name} — W/kW"

    return JsonResponse({"traces": traces})
