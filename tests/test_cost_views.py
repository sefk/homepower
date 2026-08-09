"""Catalog index and the three cost views (issue #3): true-up tracker,
peak-window bill share, cost heatmap. All render honestly against seeded
bills and, where live grid data is involved, against coverage — never
timestamp math of our own (docs/REVIEW-INSIGHTS.md).
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.utils import timezone

from billing.cycles import cycle_label, true_up_cycles
from billing.models import BillPeriod
from billing.rates import SUMMER_OFFPEAK, SUMMER_PEAK, WINTER_OFFPEAK, WINTER_PEAK
from core.coverage import record_coverage
from core.models import CoverageSpan, Sample, Series, Source

LA = ZoneInfo("America/Los_Angeles")


def la(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


@pytest.fixture
def grid_series(db) -> Series:
    source = Source.objects.create(
        slug="eagle",
        name="Grid (PG&E meter via Eagle 3)",
        kind=Source.Kind.GRID,
        poll_interval_s=60,
        native_resolution_s=60,
    )
    return Series.objects.create(source=source, metric="demand_w", unit="W")


class TestIndex:
    def test_lists_analyses_and_answers_200(self, db, client):
        resp = client.get("/")
        assert resp.status_code == 200
        content = resp.content.decode()
        # built views, grouped
        assert "Cost &amp; TOU" in content
        assert "True-up tracker" in content
        assert "Peak-window bill share" in content
        assert "Cost heatmap" in content
        assert "Grid demand" in content
        assert "Solar production" in content
        assert "Data health" in content
        # each entry states the question it answers
        assert "Where is this true-up cycle heading" in content
        # not-yet-built views are present but dimmed, with a reason
        assert "Peak decomposition" in content
        assert "EV charge sessions" in content
        assert "needs the Eagle 3" in content

    def test_entries_link_to_their_views(self, db, client):
        resp = client.get("/")
        content = resp.content.decode()
        assert '/trueup/"' in content
        assert '/peak/"' in content
        assert '/costmap/"' in content
        assert '/grid/"' in content
        assert '/solar/"' in content
        assert '/health/"' in content


class TestCycles:
    """billing.cycles: the April-boundary grouping rule, in isolation."""

    def test_groups_at_april_boundary(self, db):
        call_command("seed_bills")
        periods = list(BillPeriod.objects.order_by("end_date"))
        cycles = true_up_cycles(periods)
        assert [len(c) for c in cycles] == [12, 3]
        assert cycle_label(cycles[0]) == "2025–26"
        assert cycle_label(cycles[1]) == "2026–27"


class TestTrueupData:
    def test_two_cycles_overlaid_with_known_outcome(self, db, client):
        call_command("seed_bills")
        data = client.get("/trueup/data.json").json()
        cycles = data["cycles"]
        assert len(cycles) == 2

        prior, current = cycles
        assert prior["label"] == "2025–26"
        assert prior["complete"] is True
        assert prior["months"] == list(range(1, 13))
        # cumulative net kWh / $ over the complete cycle matches the bill's
        # true-up line exactly (test_billing.py pins the same totals)
        assert prior["net_kwh"][-1] == pytest.approx(1439)
        assert prior["nem_charges"][-1] == pytest.approx(458.26)

        assert current["label"] == "2026–27"
        assert current["complete"] is False
        assert current["months"] == [1, 2, 3]  # 3 periods so far

        assert data["outcome"]["label"] == "2025–26"
        assert data["outcome"]["net_kwh"] == pytest.approx(1439)
        assert data["outcome"]["nem_charges"] == pytest.approx(458.26)

    def test_no_bills_yields_no_outcome(self, db, client):
        data = client.get("/trueup/data.json").json()
        assert data["cycles"] == []
        assert data["outcome"] is None


class TestTrueupPage:
    def test_renders(self, db, client):
        call_command("seed_bills")
        resp = client.get("/trueup/")
        assert resp.status_code == 200
        assert b"True-up tracker" in resp.content


class TestPeakData:
    def test_seeded_periods_appear(self, db, client):
        call_command("seed_bills")
        data = client.get("/peak/data.json").json()
        assert len(data["labels"]) == 15
        assert data["labels"][0] == "2025-05-13"
        assert data["peak_kwh"][0] == pytest.approx(3)
        assert data["offpeak_kwh"][0] == pytest.approx(-436)


class TestPeakSplitMath:
    """core split arithmetic, independent of what 'today' happens to be."""

    def test_1kw_import_15_to_17_local_splits_at_the_peak_boundary(self, grid_series):
        from catalog.views import _peak_offpeak_wh

        day = date(2026, 8, 1)
        start = la(2026, 8, 1, 15, 0)  # 15:00-17:00 local, importing 1kW
        Sample.objects.create(series=grid_series, ts=start, duration_s=3600, value=1000.0)
        Sample.objects.create(
            series=grid_series, ts=start + timedelta(hours=1), duration_s=3600, value=1000.0
        )
        record_coverage(grid_series, start, start + timedelta(hours=2), CoverageSpan.State.LIVE)

        peak_wh, peak_cov, offpeak_wh, offpeak_cov = _peak_offpeak_wh(
            grid_series, day, timezone.get_current_timezone()
        )
        assert offpeak_wh == pytest.approx(1000.0)  # 15:00-16:00
        assert peak_wh == pytest.approx(1000.0)  # 16:00-17:00
        assert peak_cov > 0
        assert offpeak_cov > 0


class TestPeakPage:
    def test_no_live_data_shows_empty_state(self, db, client):
        resp = client.get("/peak/")
        assert resp.status_code == 200
        assert b"No live grid data yet" in resp.content

    def test_last_7_days_table_splits_peak_and_offpeak(self, grid_series, client, monkeypatch):
        frozen_today = date(2026, 8, 1)
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: frozen_today)

        start = la(2026, 8, 1, 15, 0)  # 15:00-17:00 local, 1kW import
        Sample.objects.create(series=grid_series, ts=start, duration_s=3600, value=1000.0)
        Sample.objects.create(
            series=grid_series, ts=start + timedelta(hours=1), duration_s=3600, value=1000.0
        )
        record_coverage(grid_series, start, start + timedelta(hours=2), CoverageSpan.State.LIVE)

        resp = client.get("/peak/")
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Sat Aug 1" in content
        assert "1000" in content  # both cells read 1000 Wh
        assert "unknown" in content  # the other 6 days have no coverage at all


class TestCostmapData:
    def test_uncovered_hours_are_null(self, grid_series, client):
        data = client.get("/costmap/data.json").json()
        assert len(data["z"]) == 24
        assert all(v is None for row in data["z"] for v in row)

    def test_summer_rate_applied(self, grid_series, client, monkeypatch):
        frozen_today = date(2026, 7, 15)  # July: summer
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: frozen_today)

        peak_start = la(2026, 7, 15, 17, 0)  # inside 4-9pm
        offpeak_start = la(2026, 7, 15, 10, 0)
        Sample.objects.create(series=grid_series, ts=peak_start, duration_s=3600, value=2000.0)
        Sample.objects.create(series=grid_series, ts=offpeak_start, duration_s=3600, value=1000.0)
        record_coverage(
            grid_series, peak_start, peak_start + timedelta(hours=1), CoverageSpan.State.LIVE
        )
        record_coverage(
            grid_series, offpeak_start, offpeak_start + timedelta(hours=1), CoverageSpan.State.LIVE
        )

        data = client.get("/costmap/data.json").json()
        di = data["days"].index("2026-07-15")
        assert data["z"][17][di] == pytest.approx(2.0 * SUMMER_PEAK)
        assert data["z"][10][di] == pytest.approx(1.0 * SUMMER_OFFPEAK)
        # every other hour that day is still uncovered -> null, not zero
        assert data["z"][0][di] is None

    def test_winter_rate_applied(self, grid_series, client, monkeypatch):
        frozen_today = date(2026, 1, 15)  # January: winter
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: frozen_today)

        peak_start = la(2026, 1, 15, 18, 0)
        offpeak_start = la(2026, 1, 15, 6, 0)
        Sample.objects.create(series=grid_series, ts=peak_start, duration_s=3600, value=3000.0)
        Sample.objects.create(series=grid_series, ts=offpeak_start, duration_s=3600, value=500.0)
        record_coverage(
            grid_series, peak_start, peak_start + timedelta(hours=1), CoverageSpan.State.LIVE
        )
        record_coverage(
            grid_series, offpeak_start, offpeak_start + timedelta(hours=1), CoverageSpan.State.LIVE
        )

        data = client.get("/costmap/data.json").json()
        di = data["days"].index("2026-01-15")
        assert data["z"][18][di] == pytest.approx(3.0 * WINTER_PEAK)
        assert data["z"][6][di] == pytest.approx(0.5 * WINTER_OFFPEAK)


class TestCostmapPage:
    def test_renders(self, db, client):
        resp = client.get("/costmap/")
        assert resp.status_code == 200
        assert b"Cost heatmap" in resp.content
        assert b"bill-derived approximations" in resp.content
