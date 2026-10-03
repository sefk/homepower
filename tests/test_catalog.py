"""Catalog views: health and solar render honestly, gaps stay gaps."""

from datetime import timedelta

import pytest
from django.utils import timezone

from core.coverage import record_coverage
from core.models import CollectorRun, CoverageSpan, Sample, Source

from .conftest import utc


@pytest.fixture
def solar_day(series):
    """A morning of 1-minute production with a 30-minute hole at 10:00."""
    for minute in range(0, 180):
        if 60 <= minute < 90:  # collector down 10:00–10:30 UTC
            continue
        Sample.objects.create(
            series=series,
            ts=utc(2026, 8, 1, 9, 0) + timedelta(minutes=minute),
            duration_s=60,
            value=500.0 + minute,
        )
    record_coverage(series, utc(2026, 8, 1, 9, 0), utc(2026, 8, 1, 10, 0), CoverageSpan.State.LIVE)
    record_coverage(series, utc(2026, 8, 1, 10, 30), utc(2026, 8, 1, 12, 0), CoverageSpan.State.LIVE)
    return series


class TestHealth:
    def test_renders_without_sources(self, db, client):
        resp = client.get("/health/")
        assert resp.status_code == 200
        assert b"No sources yet" in resp.content

    def test_shows_source_with_coverage_and_staleness(self, solar_day, client):
        resp = client.get("/health/")
        assert resp.status_code == 200
        assert b"Guest House Solar" in resp.content
        assert b"production_w" in resp.content
        assert b"stale" in resp.content  # last sample is from 2026-08-01

    def test_failures_before_a_success_read_as_recovered(self, solar_day, client):
        """A fixed outage must not look like an ongoing one (the whole point:
        stale errors on /health/ made a recovered Envoy look stuck)."""
        source = solar_day.source
        now = timezone.now()
        for minutes in (40, 39, 38):
            CollectorRun.objects.create(
                source=source,
                started=now - timedelta(minutes=minutes),
                ok=False,
                message="Cannot connect to host 10.10.0.222:443 [No route to host]",
            )
        CollectorRun.objects.create(source=source, started=now - timedelta(minutes=1), ok=True)

        resp = client.get("/health/")
        assert b"Recovered" in resp.content
        assert b"3 failures" in resp.content
        assert b"No route to host" not in resp.content  # stale errors stay hidden
        assert b"Failing now" not in resp.content

    def test_failures_after_last_success_still_show(self, solar_day, client):
        source = solar_day.source
        now = timezone.now()
        CollectorRun.objects.create(source=source, started=now - timedelta(minutes=30), ok=True)
        CollectorRun.objects.create(
            source=source,
            started=now - timedelta(minutes=2),
            ok=False,
            message="Cannot connect to host 10.10.0.222:443 [No route to host]",
        )

        resp = client.get("/health/")
        assert b"Failing now" in resp.content
        assert b"No route to host" in resp.content
        assert b"Recovered" not in resp.content

    def test_failures_with_no_success_ever_show(self, solar_day, client):
        CollectorRun.objects.create(
            source=solar_day.source,
            started=timezone.now() - timedelta(minutes=2),
            ok=False,
            message="Unable to connect to Envoy",
        )

        resp = client.get("/health/")
        assert b"Failing now" in resp.content
        assert b"since startup" in resp.content

    def test_recovered_failures_outside_the_window_are_dropped(self, solar_day, client):
        source = solar_day.source
        now = timezone.now()
        CollectorRun.objects.create(
            source=source,
            started=now - timedelta(days=30),
            ok=False,
            message="ancient history",
        )
        CollectorRun.objects.create(source=source, started=now - timedelta(minutes=1), ok=True)

        resp = client.get("/health/")
        assert b"Recovered" not in resp.content
        assert b"ancient history" not in resp.content

    def test_index_no_longer_redirects(self, db, client):
        # / is the catalog index now (see tests/test_cost_views.py); this
        # just guards against the old redirect-to-health behavior coming back.
        resp = client.get("/")
        assert resp.status_code == 200
        assert not hasattr(resp, "url")


class TestSolarData:
    def test_gap_becomes_null_never_bridged(self, solar_day, client):
        resp = client.get("/solar/data.json?date=2026-08-01")
        trace = resp.json()["traces"][0]
        # exactly one break, at the 30-minute hole
        assert trace["y"].count(None) == 1
        gap_at = trace["y"].index(None)
        assert trace["x"][gap_at] is None
        # values on both sides survive
        assert trace["y"][gap_at - 1] == 559.0  # 09:59 UTC
        assert trace["y"][gap_at + 1] == 590.0  # 10:30 UTC

    def test_fine_resolution_is_not_stepped(self, solar_day, client):
        resp = client.get("/solar/data.json?date=2026-08-01")
        assert resp.json()["traces"][0]["stepped"] is False

    def test_single_missed_interval_breaks_line(self, series, client):
        """Samples exactly 2x resolution apart = one missing interval — the
        line must break there, not draw across the unknown stretch."""
        Sample.objects.create(series=series, ts=utc(2026, 8, 1, 10, 0), duration_s=60, value=100.0)
        Sample.objects.create(series=series, ts=utc(2026, 8, 1, 10, 2), duration_s=60, value=120.0)
        trace = client.get("/solar/data.json?date=2026-08-01").json()["traces"][0]
        assert trace["y"].count(None) == 1

    def test_bad_date_404s(self, db, client):
        assert client.get("/solar/data.json?date=nope").status_code == 404


class TestSolarPage:
    def test_renders_with_hourly_table(self, solar_day, client):
        resp = client.get("/solar/?date=2026-08-01")
        assert resp.status_code == 200
        assert b"Hourly energy" in resp.content
        assert b"unknown" in resp.content  # uncovered hours say so, not zero

    def test_hourly_table_per_source(self, solar_day, client):
        """Two arrays -> two labeled tables; nothing nondeterministic."""
        from core.models import Series, Source

        main = Source.objects.create(
            slug="solaredge", name="Main House Solar",
            kind=Source.Kind.SOLAR, poll_interval_s=900, native_resolution_s=900,
        )
        Series.objects.create(source=main, metric="production_w", unit="W")
        resp = client.get("/solar/?date=2026-08-01")
        assert resp.content.count(b"Hourly energy \xe2\x80\x94") == 2
        assert b"Guest House Solar" in resp.content
        assert b"Main House Solar" in resp.content
