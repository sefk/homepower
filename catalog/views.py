"""Catalog views: the opinionated analyses (PRD: the catalog is the product).

The index page lists every analysis with the question it answers. Views
render gaps as gaps and cost views apply bill-derived TOU rates — nothing
here interpolates across a hole or treats missing samples as zero.
"""

import json
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db.models import Min, Q
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from billing import rates
from billing.cycles import cycle_label, true_up_cycles
from billing.models import BillPeriod, GasBillPeriod, UtilityRate
from billing.rates import PEAK_END_HOUR, PEAK_START_HOUR, rate_for, rates_for
from catalog import grafana
from catalog.ev import sessions as ev_sessions
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
EV_LOOKBACK_DAYS = 180  # /ev/ trailing window for charge-session derivation

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
        "name": "EV",
        "entries": [
            {
                "title": "EV charge sessions",
                "url_name": "catalog:ev",
                "question": "What did each charge session cost, and what would shifting past 9pm have saved?",
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
    ],
}


# Loads drawn on the home diagram: (node key, source slug, metric).
HOME_LIVE = {
    "solaredge": ("solaredge", "production_w"),
    "envoy": ("envoy", "production_w"),
    "grid": ("eagle", "demand_w"),
    "ev": ("tesla", "ev_charge_power_w"),
    "hottub": ("hottub_est", "power_w"),
    "navien": ("navien", "power_w"),
    "studio": ("studio", "power_w"),
    "closet": ("closet", "power_w"),
}


HOME_IDLE_W = 10  # below this a wire is drawn idle rather than flowing


# The small number after the slash is the mean power for this hour of the day
# over the previous HOME_AVG_DAYS days (gas: its daily mean over that many
# posted days). HOME_AVG_MIN_COVERAGE is the share of those hours that must
# have been seen for it to show at all. HOME_AVG_FULL_COVERAGE is the share
# every input of a derived average must reach: sums of means only add up over
# the same stretches of time.
HOME_AVG_DAYS = 30
HOME_AVG_MIN_COVERAGE = 0.4
HOME_AVG_FULL_COVERAGE = 0.9
# A sample starting before an hour can still overlap it. Only the slow
# collectors write samples this long (Tesla charging 5 min, SolarEdge 15,
# Tesla idle 30), so look back an hour for those alone.
HOME_LONG_SAMPLE_S = 300
HOME_LOOKBACK = timedelta(hours=1)
HOME_GAS_MAX_AGE_DAYS = 5  # PG&E posts a day or two late; older is a stalled feed
def _fmt_watts(value, prior=None):
    """Display parts for a power reading and the average, in one
    unit chosen from the reading (the average's from itself when there's no
    reading). Unknown values come back as dashes."""
    ref = value if value is not None else prior
    kilo = ref is not None and abs(ref) >= 1000

    def part(v, signed=False):
        if v is None:
            return None
        sign = "−" if signed and v < 0 else ""
        v = abs(v)
        return sign + (f"{v / 1000:.2f}" if kilo else f"{v:,.0f}")

    return {
        "text": part(value) or "—",
        "unit": "" if ref is None else ("kW" if kilo else "W"),
        "known": value is not None,
        "prior": part(prior, signed=True) or "—",
    }


def _hour_windows(now, days=HOME_AVG_DAYS):
    """This local hour of the day on each of the previous `days` days, as
    (start, end) UTC pairs, oldest first. Built from local wall-clock time so
    a daylight-saving change doesn't slide the hour."""
    local = timezone.localtime(now)
    tz = timezone.get_current_timezone()
    windows = []
    for d in range(days, 0, -1):
        day = local.date() - timedelta(days=d)
        start = datetime.combine(day, time(local.hour), tzinfo=tz).astimezone(dt_timezone.utc)
        windows.append((start, start + timedelta(hours=1)))
    return windows


def _avg_watts(slug, metric, now):
    """(mean power, seen fraction) for this hour of the day over the previous
    HOME_AVG_DAYS days.

    The mean is time-weighted over the samples that exist, clipped to each
    hour, so unseen time is left out rather than counted as zero; the seen
    fraction is that time over all the hours. None when under
    HOME_AVG_MIN_COVERAGE was seen, so an outage never reads as a quiet day
    (and a source that records nothing at this hour, like SolarEdge at night,
    has no average rather than a zero one)."""
    windows = _hour_windows(now)
    series = Sample.objects.filter(series__source__slug=slug, series__metric=metric)
    watt_seconds = seconds = 0.0
    # One small indexed query per hour: a single OR across all thirty is
    # ~100x slower (SQLite scans instead of seeking).
    for start, end in windows:
        rows = series.filter(
            Q(ts__gte=start, ts__lt=end)
            | Q(ts__gte=start - HOME_LOOKBACK, ts__lt=start, duration_s__gte=HOME_LONG_SAMPLE_S)
        ).values_list("ts", "duration_s", "value")
        for ts, duration_s, value in rows:
            overlap = (min(end, ts + timedelta(seconds=duration_s)) - max(start, ts)).total_seconds()
            if overlap > 0:
                watt_seconds += value * overlap
                seconds += overlap
    seen = seconds / (len(windows) * 3600)
    if seen < HOME_AVG_MIN_COVERAGE:
        return None, seen
    return watt_seconds / seconds, seen


def _with_derived(vals, strict=False):
    """Add solar, house and unmetered to the per-source readings, the way
    the Sinks dashboard derives them: house load = solar + grid (grid is
    signed, negative when exporting); unknown if any input is."""
    parts = [vals["solaredge"], vals["envoy"], vals["grid"]]
    vals["solar"] = None if None in parts[:2] else parts[0] + parts[1]
    vals["house"] = None if None in parts else sum(parts)
    metered = [vals[k] for k in ("ev", "hottub", "navien", "studio", "closet")]
    # Live, a load with no reading is left out of the subtraction. Averages
    # are stricter: subtracting a mean that covers a different stretch of
    # time than the house mean would be arithmetic across windows.
    vals["unmetered"] = (
        None
        if vals["house"] is None or (strict and None in metered)
        else max(vals["house"] - sum(v for v in metered if v is not None), 0)
    )
    return vals


def _live_watts(slug, metric, now):
    """Latest reading if it is still fresh, else None (unknown, never zero).

    A sample is current while `now` is inside its own interval plus the
    source's staleness allowance, so a 15-minute SolarEdge point still
    counts for its quarter hour."""
    sample = (
        Sample.objects.filter(series__source__slug=slug, series__metric=metric)
        .select_related("series__source")
        .order_by("-ts")
        .first()
    )
    if sample is None:
        return None
    allowance = timedelta(seconds=sample.duration_s + sample.series.source.poll_interval_s * (STALE_POLLS - 1))
    if now - sample.ts > allowance:
        return None
    return sample.value


def _gas_daily(now):
    """(day, mean thermal W that day, mean over the last HOME_AVG_DAYS posted days).

    Gas arrives as whole days, a day or two late, so there is no "now": the
    latest posted day stands in for it. The series is already in Wh of heat
    (therms are energy), so a day's mean power is its Wh over its hours. None
    when the latest day is too old to call current."""
    days = list(
        Sample.objects.filter(series__source__slug="pge_gas", series__metric="gas_wh")
        .order_by("-ts")[:HOME_AVG_DAYS]
    )
    if not days or now - days[0].ts > timedelta(days=HOME_GAS_MAX_AGE_DAYS):
        return None
    latest = days[0].value / (days[0].duration_s / 3600)
    mean = sum(d.value for d in days) / (sum(d.duration_s for d in days) / 3600)
    return timezone.localtime(days[0].ts).date(), latest, mean


def home(request):
    """House power diagram: what feeds the house, what draws from it, and
    which meter sees each piece, with the latest reading where one is fresh."""
    now = timezone.now()
    live = _with_derived({k: _live_watts(slug, m, now) for k, (slug, m) in HOME_LIVE.items()})
    avgs = {k: _avg_watts(slug, m, now) for k, (slug, m) in HOME_LIVE.items()}
    short = {k: cov < HOME_AVG_FULL_COVERAGE for k, (_, cov) in avgs.items()}
    # Derived averages only from inputs seen for (nearly) the whole window:
    # sums and differences of means over different stretches of time are
    # wrong, not just rough (SolarEdge records by day, so its mean is a
    # daytime one and the grid's is not).
    prior = {k: avg for k, (avg, _) in avgs.items()}
    prior |= {
        k: v
        for k, v in _with_derived(
            {k: None if short[k] else avg for k, (avg, _) in avgs.items()}, strict=True
        ).items()
        if k in ("solar", "house", "unmetered")
    }
    gas = _gas_daily(now)
    grid = live["grid"]
    grid_dir = None if grid is None or abs(grid) < HOME_IDLE_W else ("in" if grid > 0 else "out")
    ctx = {
        "section": "home",
        # the Grafana dashboard that charts each node over time
        "graf": {
            "solaredge": grafana.url(request, "homepower-sources"),
            "envoy": grafana.url(request, "homepower-sources"),
            "grid": grafana.url(request, "homepower-live"),
            "house": grafana.url(request, "homepower-sinks"),
            "ev": grafana.url(request, "homepower-sinks", loads="Tesla"),
            "hottub": grafana.url(request, "homepower-sinks", loads="Hot Tub (estimated)"),
            "navien": grafana.url(request, "homepower-sinks", loads="Water Heater"),
            "studio": grafana.url(request, "homepower-sinks", loads="Mac Studio"),
            "closet": grafana.url(request, "homepower-sinks", loads="Network Closet"),
            "unmetered": grafana.url(request, "homepower-sink-summary"),
            "gas": grafana.url(request, "homepower-gas"),
        },
        "show": {key: _fmt_watts(value, prior[key]) for key, value in live.items()}
        | {"gas": _fmt_watts(*gas[1:]) if gas else _fmt_watts(None)},
        "gas_how": f"{gas[0]:%b} {gas[0].day} average · pge.com, daily" if gas else "pge.com · no recent reading",
        # which wires carry current right now (below the floor reads as idle)
        "on": {key: value is not None and abs(value) >= HOME_IDLE_W for key, value in live.items()},
        "grid_dir": grid_dir,
        "grid_label": {"in": " · importing", "out": " · exporting", None: ""}[grid_dir] or (" · idle" if grid is not None else ""),
    }
    return render(request, "catalog/home.html", ctx)


def index(request):
    """The analysis catalog — every view, grouped, with the question it answers."""
    return render(
        request,
        "catalog/index.html",
        {"groups": CATALOG_GROUPS, "waiting": WAITING_GROUP, "section": "catalog"},
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

        # Only failures since the last success are still happening; anything
        # older is history the collector has already recovered from. Showing
        # both alike made a fixed outage look identical to an ongoing one.
        last_ok = source.runs.filter(ok=True).order_by("-started").first()
        failures = source.runs.filter(ok=False)
        if last_ok is not None:
            failures = failures.filter(started__gt=last_ok.started)
        resolved = source.runs.filter(ok=False, started__gte=window_start)
        if last_ok is not None:
            resolved = resolved.filter(started__lte=last_ok.started)
        resolved_last = resolved.order_by("-started").first()

        sources.append(
            {
                "source": source,
                "push": source.slug == "eagle",  # push sources have no poll loop
                "series_rows": series_rows,
                "last_sample": last_sample,
                "last_run": source.runs.order_by("-started").first() if not series_rows or source.kind == Source.Kind.ESTIMATE else None,
                "stale": (
                    # A source that writes no samples (pge_rates writes rate
                    # rows) is only as fresh as its last good run -- and so is
                    # a derived estimate, whose newest sample is the start of
                    # a segment that may be hours long.
                    (last_ok is None or now - last_ok.started > stale_after)
                    if not series_rows or source.kind == Source.Kind.ESTIMATE
                    else last_sample is None or now - last_sample.ts > stale_after
                ),
                "failures": list(failures.order_by("-started")[:5]),
                "recovered_count": resolved.count(),
                "recovered_last": resolved_last,
                "recovered_at": last_ok.started if last_ok else None,
            }
        )

    return render(
        request,
        "catalog/health.html",
        {
            "section": "health",
            "sources": sources,
            "missing_adjustments": [
                f"{season} {period.replace('offpeak', 'off-peak')}"
                for season, period in rates.missing_adjustments()
            ],
            "last_rate_change": UtilityRate.objects.exclude(source="seed")
            .filter(effective_from__gt=rates.OPOWER_BASIS_START)
            .order_by("-effective_from")
            .first(),
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

    # An array with nothing in the window would otherwise just be a missing
    # line. Say so, with its last reading -- SolarEdge's portal can lag the
    # morning by an hour or more while Grafana's trailing 24h still shows
    # yesterday.
    silent = []
    for series in (
        Series.objects.filter(source__kind=Source.Kind.SOLAR, metric="production_w")
        .select_related("source")
        .order_by("source__slug")
    ):
        if Sample.objects.filter(series=series, ts__gte=start, ts__lt=end).exists():
            continue
        last = Sample.objects.filter(series=series, ts__lt=end).order_by("-ts").first()
        silent.append({"source": series.source, "last_ts": last.ts if last else None})

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
            "silent": silent,
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
            "rate_eras": rates.era_table(),
            "missing_adjustments": [
                f"{season} {period.replace('offpeak', 'off-peak')}"
                for season, period in rates.missing_adjustments()
            ],
            "bill_2026_07": {
                "pge_peak": rates.PGE_SUMMER_PEAK_2026,
                "pge_offpeak": rates.PGE_SUMMER_OFFPEAK_2026,
                "baseline_credit": rates.BASELINE_CREDIT_2026,
                "generation_credit": rates.PGE_GENERATION_CREDIT_2026,
                "pcia": rates.PCIA_2026,
                "westlight_peak": rates.WESTLIGHT_SUMMER_PEAK_2026,
                "westlight_offpeak": rates.WESTLIGHT_SUMMER_OFFPEAK_2026,
                "westlight_peak_jul": rates.WESTLIGHT_SUMMER_PEAK_2026_07,
                "westlight_offpeak_jul": rates.WESTLIGHT_SUMMER_OFFPEAK_2026_07,
            },
            "bill_2026_04": {
                "pge_peak": rates.PGE_WINTER_PEAK_2026,
                "pge_offpeak": rates.PGE_WINTER_OFFPEAK_2026,
                "baseline_credit": rates.BASELINE_CREDIT_2026,
                "generation_credit": rates.PGE_GENERATION_CREDIT_2026_04,
                "pcia": rates.PCIA_2026_04,
                "cca_peak": rates.WESTLIGHT_WINTER_PEAK_2026,
                "cca_offpeak": rates.WESTLIGHT_WINTER_OFFPEAK_2026,
            },
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


def ev(request):
    """What did each charge session cost, and what would shifting past
    9pm have saved? The table is the view -- no chart for v1 (a session
    list of a dozen rows says more as numbers than as a sparse bar chart)."""
    series = Series.objects.filter(
        source__kind=Source.Kind.EV, metric="ev_charge_power_w"
    ).first()

    rows = []
    if series:
        end = timezone.now()
        start = end - timedelta(days=EV_LOOKBACK_DAYS)
        for s in ev_sessions(series, start, end):
            total_min = round(s.duration_s / 60)
            hours, mins = divmod(total_min, 60)
            duration_label = f"{hours}h {mins}m" if hours else f"{mins}m"
            rows.append(
                {
                    "start": s.start,
                    "duration_label": duration_label,
                    "kwh": s.kwh,
                    "actual_cost": s.actual_cost,
                    "shifted_cost": s.counterfactual_cost,
                    "saved": s.savings,
                }
            )

    return render(
        request,
        "catalog/ev.html",
        {
            "section": "ev",
            "has_source": series is not None,
            "sessions": rows,
            "total_savings": sum(r["saved"] for r in rows),
            "lookback_days": EV_LOOKBACK_DAYS,
        },
    )
