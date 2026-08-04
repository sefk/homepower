"""Catalog views: the opinionated analyses (PRD: the catalog is the product).

M1 ships two entries: data health (first-class, per PRD) and ADU solar
production. Both render gaps as gaps — nothing here interpolates.
"""

import json
from datetime import datetime, time, timedelta

from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from core import coverage
from core.aggregate import energy_wh
from core.models import CollectorRun, Sample, Series, Source

# A source is stale when it has been silent this many poll intervals.
STALE_POLLS = 3
HEALTH_WINDOW_DAYS = 7


def index(request):
    return redirect("catalog:health")


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


def _solar_window(request):
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
    """ADU solar production; hourly table twin rides along."""
    day, span, days, start, end = _solar_window(request)

    hours = []
    series = Series.objects.filter(
        source__kind=Source.Kind.SOLAR, metric="production_w"
    ).first()
    if series and days == 1:
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
            "hours": hours,
        },
    )


def solar_data(request):
    """Plotly payload. Gaps become nulls so lines break instead of bridging."""
    _, _, _, start, end = _solar_window(request)
    tz = timezone.get_current_timezone()

    traces = []
    solar_series = Series.objects.filter(source__kind=Source.Kind.SOLAR).select_related(
        "source"
    )
    for series in solar_series:
        resolution = series.source.native_resolution_s
        xs, ys = [], []
        prev_ts = None
        samples = Sample.objects.filter(
            series=series, ts__gte=start, ts__lt=end
        ).order_by("ts")
        for s in samples.iterator():
            if prev_ts is not None and (s.ts - prev_ts).total_seconds() > 2 * resolution:
                # a hole: break the line, never bridge it
                xs.append(None)
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

    return JsonResponse({"traces": traces})
