"""Catalog views: the opinionated analyses (PRD: the catalog is the product).

The index page lists every analysis with the question it answers. Views
render gaps as gaps and cost views apply bill-derived TOU rates — nothing
here interpolates across a hole or treats missing samples as zero.
"""

import json
from datetime import datetime, time, timedelta

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from billing.cycles import cycle_label, true_up_cycles
from billing.models import BillPeriod
from billing.rates import PEAK_END_HOUR, PEAK_START_HOUR, rate_for
from core import coverage
from core.aggregate import energy_wh, energy_wh_split
from core.models import CollectorRun, Sample, Series, Source

# A source is stale when it has been silent this many poll intervals.
STALE_POLLS = 3
HEALTH_WINDOW_DAYS = 7
PEAK_TABLE_DAYS = 7  # /peak/ live table: trailing days from Eagle data
COSTMAP_DAYS = 30  # /costmap/ heatmap: trailing days

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
        "name": "Solar",
        "entries": [
            {
                "title": "Solar production",
                "url_name": "catalog:solar",
                "question": "How much are the arrays producing right now, and where's the data missing?",
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
            "why": "needs the Eagle 3 (sub-minute whole-home demand)",
        },
        {
            "title": "Load signatures",
            "question": "Isolate the hot tub, dryer, and EV charging by their on/off shape.",
            "why": "needs sub-minute resolution from the Eagle 3",
        },
        {
            "title": "Overnight floor",
            "question": "Is the 3–5am baseline load rising month over month?",
            "why": "needs the Eagle 3 for whole-home demand",
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
            "title": "Self-consumption",
            "question": "What fraction of generation is used on-site vs. exported, by season?",
            "why": "needs the Eagle 3 to net solar against whole-home demand",
        },
        {
            "title": "Export timing vs. peak windows",
            "question": "Is solar exporting at 1pm while the house imports at 6pm?",
            "why": "needs the Eagle 3 to net solar against whole-home demand",
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
        {
            "title": "Electrification modeling",
            "question": "What would winter gas load cost as heat-pump electric, and how big a shortfall would the array have?",
            "why": "modeled analysis, not yet built",
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
    """Whole-home grid demand; hourly import/export table twin rides along."""
    day, span, days, start, end = _window(request)

    hours = []
    series = Series.objects.filter(
        source__kind=Source.Kind.GRID, metric="demand_w"
    ).first()
    if series and days == 1:
        for h in range(24):
            h_start = start + timedelta(hours=h)
            result = energy_wh_split(series, h_start, h_start + timedelta(hours=1))
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
    for i, cycle in enumerate(cycles):
        complete = i < len(cycles) - 1  # every cycle but the last has closed
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


def _peak_offpeak_wh(series: Series, local_day, tz):
    """Imported Wh and coverage for a local day, split at the 4-9pm window.

    Off-peak is two sub-windows (midnight-4pm, 9pm-midnight); their imported
    Wh sums and their coverage combines as a duration-weighted average — the
    same arithmetic covered_fraction does over a single window, just applied
    across the two pieces (never our own gap detection — coverage stays the
    one authority, per docs/REVIEW-INSIGHTS.md).
    """
    day_start = datetime.combine(local_day, time.min, tzinfo=tz)
    peak_start = datetime.combine(local_day, time(hour=PEAK_START_HOUR), tzinfo=tz)
    peak_end = datetime.combine(local_day, time(hour=PEAK_END_HOUR), tzinfo=tz)
    next_day_start = day_start + timedelta(days=1)

    peak = energy_wh_split(series, peak_start, peak_end)
    off_morning = energy_wh_split(series, day_start, peak_start)
    off_evening = energy_wh_split(series, peak_end, next_day_start)

    dur_morning = (peak_start - day_start).total_seconds()
    dur_evening = (next_day_start - peak_end).total_seconds()
    dur_off = dur_morning + dur_evening
    offpeak_wh = off_morning.imported_wh + off_evening.imported_wh
    offpeak_coverage = (
        off_morning.coverage * dur_morning + off_evening.coverage * dur_evening
    ) / dur_off

    return peak.imported_wh, peak.coverage, offpeak_wh, offpeak_coverage


def peak(request):
    """What share of the bill is the 4-9pm window? Billed peak/off-peak kWh
    per period, plus a live 7-day table of the same split from Eagle data."""
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    days = [today - timedelta(days=i) for i in range(PEAK_TABLE_DAYS - 1, -1, -1)]

    series = Series.objects.filter(source__kind=Source.Kind.GRID, metric="demand_w").first()
    rows = []
    if series:
        for day in days:
            peak_wh, peak_cov, offpeak_wh, offpeak_cov = _peak_offpeak_wh(series, day, tz)
            rows.append(
                {
                    "day": day,
                    "peak_wh": peak_wh,
                    "peak_coverage_pct": 100 * peak_cov,
                    "offpeak_wh": offpeak_wh,
                    "offpeak_coverage_pct": 100 * offpeak_cov,
                }
            )

    return render(request, "catalog/peak.html", {"section": "catalog", "rows": rows})


def peak_data(request):
    periods = BillPeriod.objects.order_by("end_date")
    return JsonResponse(
        {
            "labels": [p.end_date.isoformat() for p in periods],
            "peak_kwh": [p.peak_kwh for p in periods],
            "offpeak_kwh": [p.offpeak_kwh for p in periods],
        }
    )


def costmap(request):
    """Which hours cost the money? Hour x day heatmap of import cost,
    bill-derived rates applied; uncovered hours are blank, never zero."""
    return render(request, "catalog/costmap.html", {"section": "catalog"})


def costmap_data(request):
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    days = [today - timedelta(days=i) for i in range(COSTMAP_DAYS - 1, -1, -1)]

    series = Series.objects.filter(source__kind=Source.Kind.GRID, metric="demand_w").first()
    z = [[None] * len(days) for _ in range(24)]
    if series:
        for di, day in enumerate(days):
            day_start = datetime.combine(day, time.min, tzinfo=tz)
            for h in range(24):
                h_start = day_start + timedelta(hours=h)
                result = energy_wh_split(series, h_start, h_start + timedelta(hours=1))
                if result.coverage > 0:  # coverage == 0 stays null, never zero
                    z[h][di] = result.imported_wh / 1000 * rate_for(h_start)

    return JsonResponse(
        {"days": [d.isoformat() for d in days], "hours": list(range(24)), "z": z}
    )
