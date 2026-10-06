"""Grid catalog view: signed demand, import/export split, gaps stay gaps."""

from datetime import timedelta

import pytest

from core.coverage import record_coverage
from core.models import CoverageSpan, Sample, Series, Source

from .conftest import utc


@pytest.fixture
def grid_day(db):
    """A demand day: importing 1 kW, a hole, then exporting 2 kW."""
    source = Source.objects.create(
        slug="eagle",
        name="PG&E Grid",
        kind=Source.Kind.GRID,
        poll_interval_s=60,
        native_resolution_s=60,
    )
    series = Series.objects.create(source=source, metric="demand_w", unit="W")
    t0 = utc(2026, 8, 1, 9, 0)
    for minute in range(0, 60):  # 09:00-10:00 importing
        Sample.objects.create(
            series=series, ts=t0 + timedelta(minutes=minute), duration_s=60, value=1000.0
        )
    for minute in range(120, 180):  # 11:00-12:00 exporting (10:00-11:00 is a hole)
        Sample.objects.create(
            series=series, ts=t0 + timedelta(minutes=minute), duration_s=60, value=-2000.0
        )
    record_coverage(series, t0, t0 + timedelta(hours=1), CoverageSpan.State.LIVE)
    record_coverage(
        series, t0 + timedelta(hours=2), t0 + timedelta(hours=3), CoverageSpan.State.LIVE
    )
    return series


class TestGridData:
    def test_gap_becomes_null(self, grid_day, client):
        trace = client.get("/grid/data.json?date=2026-08-01").json()["traces"][0]
        assert trace["y"].count(None) == 1

    def test_only_demand_not_counters(self, grid_day, client):
        Series.objects.create(
            source=grid_day.source, metric="energy_delivered_wh", unit="Wh"
        )
        traces = client.get("/grid/data.json?date=2026-08-01").json()["traces"]
        assert [t["metric"] for t in traces] == ["demand_w"]


class TestGridPage:
    def test_hourly_split_and_unknown_hours(self, grid_day, client):
        resp = client.get("/grid/?date=2026-08-01")
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Grid demand" in content
        assert "unknown" in content  # the 10:00 hole and the uncovered rest of day

    def test_split_math(self, grid_day):
        from core.aggregate import energy_wh_split

        t0 = utc(2026, 8, 1, 9, 0)
        imp = energy_wh_split(grid_day, t0, t0 + timedelta(hours=1))
        assert imp.imported_wh == pytest.approx(1000.0)
        assert imp.exported_wh == 0.0
        exp = energy_wh_split(grid_day, t0 + timedelta(hours=2), t0 + timedelta(hours=3))
        assert exp.exported_wh == pytest.approx(2000.0)
        assert exp.imported_wh == 0.0


@pytest.mark.django_db
class TestTariffCard:
    def test_explains_nem_and_lists_rates(self, client):
        from billing.rates import SUMMER_OFFPEAK, SUMMER_PEAK

        content = client.get("/grid/").content.decode()
        assert 'id="tariff"' in content
        assert "E-TOU-C" in content
        assert "Net Energy Metering" in content
        assert f"${SUMMER_PEAK:.3f}" in content
        assert f"${SUMMER_OFFPEAK:.3f}" in content
