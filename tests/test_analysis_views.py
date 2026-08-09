"""The four analyses added by issue #4: baseline (overnight floor),
electrify (gas -> heat-pump modeling), selfuse (self-consumption), and
solarhealth (per-kW comparison). Gap/coverage semantics come from
core.coverage only, and windows are built the same way the existing cost
views build them -- same-tzinfo local datetimes, normalized to UTC inside
coverage/aggregate (docs/REVIEW-INSIGHTS.md).
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command

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


@pytest.fixture
def adu_series(db) -> Series:
    source = Source.objects.create(
        slug="envoy",
        name="ADU solar (Enphase Envoy)",
        kind=Source.Kind.SOLAR,
        poll_interval_s=60,
        native_resolution_s=60,
    )
    return Series.objects.create(source=source, metric="production_w", unit="W")


@pytest.fixture
def main_series(db) -> Series:
    source = Source.objects.create(
        slug="solaredge",
        name="Main solar (SolarEdge cloud)",
        kind=Source.Kind.SOLAR,
        poll_interval_s=900,
        native_resolution_s=900,
    )
    return Series.objects.create(source=source, metric="production_w", unit="W")


class TestBaselineData:
    def test_covered_day_yields_true_3_5am_minimum(self, grid_series, client, monkeypatch):
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: date(2026, 8, 1))
        day = date(2026, 7, 30)
        start = la(2026, 7, 30, 3, 0)
        for i, value in enumerate([300.0, 150.0, 400.0, 250.0]):  # true min is 150
            Sample.objects.create(
                series=grid_series, ts=start + timedelta(minutes=30 * i), duration_s=1800, value=value
            )
        record_coverage(grid_series, start, start + timedelta(hours=2), CoverageSpan.State.LIVE)

        data = client.get("/baseline/data.json").json()
        idx = data["days"].index(day.isoformat())
        assert data["min_w"][idx] == pytest.approx(150.0)
        assert data["qualifying_days"] == 1

    def test_day_with_partial_3_5am_coverage_is_null(self, grid_series, client, monkeypatch):
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: date(2026, 8, 1))
        day = date(2026, 7, 29)
        start = la(2026, 7, 29, 3, 0)
        # only the first 30 of 120 minutes covered -- 25%, below the 90% floor
        Sample.objects.create(series=grid_series, ts=start, duration_s=1800, value=50.0)
        record_coverage(grid_series, start, start + timedelta(minutes=30), CoverageSpan.State.LIVE)

        data = client.get("/baseline/data.json").json()
        idx = data["days"].index(day.isoformat())
        assert data["min_w"][idx] is None
        assert data["qualifying_days"] == 0


class TestBaselinePage:
    def test_empty_state_when_no_qualifying_days(self, db, client):
        resp = client.get("/baseline/")
        assert resp.status_code == 200
        assert b"Not enough overnight data" in resp.content

    def test_chart_renders_once_a_day_qualifies(self, grid_series, client, monkeypatch):
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: date(2026, 8, 1))
        start = la(2026, 7, 30, 3, 0)
        Sample.objects.create(series=grid_series, ts=start, duration_s=7200, value=100.0)
        record_coverage(grid_series, start, start + timedelta(hours=2), CoverageSpan.State.LIVE)

        resp = client.get("/baseline/")
        assert resp.status_code == 200
        assert b"Not enough overnight data" not in resp.content


class TestElectrifyData:
    def test_therms_to_kwh_at_default_cop(self, db, client):
        call_command("seed_bills")
        data = client.get("/electrify/data.json").json()
        idx = data["labels"].index("2025-12-16")  # 91 therms, per issue #4's worked example
        assert data["therms"][idx] == pytest.approx(91.0)
        assert data["cop"] == pytest.approx(3.0)
        assert data["heat_pump_kwh"][idx] == pytest.approx(91 * 29.3 / 3.0)

    def test_cop_query_param_scales_kwh(self, db, client):
        call_command("seed_bills")
        data = client.get("/electrify/data.json?cop=2.5").json()
        idx = data["labels"].index("2025-12-16")
        assert data["cop"] == pytest.approx(2.5)
        assert data["heat_pump_kwh"][idx] == pytest.approx(91 * 29.3 / 2.5)

    def test_cop_is_clamped_to_1_5_5_0(self, db, client):
        low = client.get("/electrify/data.json?cop=0.5").json()
        assert low["cop"] == pytest.approx(1.5)
        high = client.get("/electrify/data.json?cop=100").json()
        assert high["cop"] == pytest.approx(5.0)

    def test_bad_cop_falls_back_to_default(self, db, client):
        data = client.get("/electrify/data.json?cop=not-a-number").json()
        assert data["cop"] == pytest.approx(3.0)

    def test_gas_bill_matched_to_electric_bill_by_end_month(self, db, client):
        call_command("seed_bills")
        data = client.get("/electrify/data.json").json()
        idx = data["labels"].index("2025-12-16")  # gas bill ending Dec 16
        assert data["net_kwh"][idx] == pytest.approx(1037)  # electric bill ending Dec 15

    def test_shortfall_arithmetic(self, db, client):
        call_command("seed_bills")
        data = client.get("/electrify/data.json").json()
        # seeded gas bills sum to 366 therms/yr (matches the PRD's ~366 therms/yr figure)
        expected_annual_kwh = 366.0 * 29.3 / 3.0
        assert data["annual_heat_pump_kwh"] == pytest.approx(expected_annual_kwh)
        assert data["shortfall_kw"] == pytest.approx(expected_annual_kwh / 1450)


class TestElectrifyPage:
    def test_renders(self, db, client):
        call_command("seed_bills")
        resp = client.get("/electrify/")
        assert resp.status_code == 200
        assert b"Electrification modeling" in resp.content

    def test_cop_toggle_links_present(self, db, client):
        resp = client.get("/electrify/")
        content = resp.content.decode()
        assert "?cop=2.5" in content
        assert "?cop=3.5" in content


class TestSelfuseData:
    def test_generation_and_export_yield_self_used_and_fraction(
        self, adu_series, grid_series, client, monkeypatch
    ):
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: date(2026, 8, 5))
        day = date(2026, 8, 4)
        day_start = la(2026, 8, 4, 0, 0)
        day_end = day_start + timedelta(days=1)

        Sample.objects.create(series=adu_series, ts=day_start, duration_s=3600, value=2000.0)  # 2 kWh
        record_coverage(adu_series, day_start, day_end, CoverageSpan.State.LIVE)
        Sample.objects.create(series=grid_series, ts=day_start, duration_s=3600, value=-500.0)  # 0.5 kWh export
        record_coverage(grid_series, day_start, day_end, CoverageSpan.State.LIVE)

        data = client.get("/selfuse/data.json").json()
        idx = data["days"].index(day.isoformat())
        assert data["self_used_wh"][idx] == pytest.approx(1500.0)
        assert data["exported_wh"][idx] == pytest.approx(500.0)
        assert data["fraction"][idx] == pytest.approx(0.75)

    def test_poor_grid_coverage_is_unknown(self, adu_series, grid_series, client, monkeypatch):
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: date(2026, 8, 5))
        day = date(2026, 8, 4)
        day_start = la(2026, 8, 4, 0, 0)
        day_end = day_start + timedelta(days=1)

        Sample.objects.create(series=adu_series, ts=day_start, duration_s=3600, value=2000.0)
        record_coverage(adu_series, day_start, day_end, CoverageSpan.State.LIVE)
        # grid covered for only 1 of the day's 24 hours -- well under the 90% floor
        Sample.objects.create(series=grid_series, ts=day_start, duration_s=3600, value=-500.0)
        record_coverage(grid_series, day_start, day_start + timedelta(hours=1), CoverageSpan.State.LIVE)

        data = client.get("/selfuse/data.json").json()
        idx = data["days"].index(day.isoformat())
        assert data["self_used_wh"][idx] is None
        assert data["exported_wh"][idx] is None
        assert data["fraction"][idx] is None

    def test_poor_generation_coverage_is_unknown(self, adu_series, grid_series, client, monkeypatch):
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: date(2026, 8, 5))
        day = date(2026, 8, 4)
        day_start = la(2026, 8, 4, 0, 0)
        day_end = day_start + timedelta(days=1)

        # generation covered for only 1 hour of the day
        Sample.objects.create(series=adu_series, ts=day_start, duration_s=3600, value=2000.0)
        record_coverage(adu_series, day_start, day_start + timedelta(hours=1), CoverageSpan.State.LIVE)
        Sample.objects.create(series=grid_series, ts=day_start, duration_s=3600, value=-500.0)
        record_coverage(grid_series, day_start, day_end, CoverageSpan.State.LIVE)

        data = client.get("/selfuse/data.json").json()
        idx = data["days"].index(day.isoformat())
        assert data["self_used_wh"][idx] is None


class TestSelfusePage:
    def test_adu_only_banner_when_no_solaredge_source(self, adu_series, client):
        resp = client.get("/selfuse/")
        assert b"Main array not yet collecting" in resp.content

    def test_no_banner_once_solaredge_source_exists(self, adu_series, main_series, client):
        resp = client.get("/selfuse/")
        assert b"Main array not yet collecting" not in resp.content


class TestSolarhealthData:
    def test_per_kw_scaling_correct_for_both_sources(
        self, adu_series, main_series, client, monkeypatch
    ):
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: date(2026, 8, 5))
        ts = la(2026, 8, 4, 12, 0)
        Sample.objects.create(series=adu_series, ts=ts, duration_s=60, value=875.0)  # 1.75 kW -> 500 W/kW
        record_coverage(adu_series, ts, ts + timedelta(minutes=1), CoverageSpan.State.LIVE)
        Sample.objects.create(series=main_series, ts=ts, duration_s=900, value=4312.5)  # 8.625 kW -> 500 W/kW
        record_coverage(main_series, ts, ts + timedelta(minutes=15), CoverageSpan.State.LIVE)

        traces = client.get("/solarhealth/data.json").json()["traces"]
        adu_trace = next(t for t in traces if "ADU" in t["name"])
        main_trace = next(t for t in traces if "Main" in t["name"])
        assert adu_trace["y"][0] == pytest.approx(500.0)
        assert main_trace["y"][0] == pytest.approx(500.0)


class TestSolarhealthPage:
    def test_single_trace_message_when_no_solaredge_source(self, adu_series, client):
        resp = client.get("/solarhealth/")
        assert b"comparison activates" in resp.content

    def test_no_message_once_solaredge_source_exists(self, adu_series, main_series, client):
        resp = client.get("/solarhealth/")
        assert b"comparison activates" not in resp.content


class TestIndexLive:
    def test_new_analyses_are_live_with_links(self, db, client):
        resp = client.get("/")
        content = resp.content.decode()
        for title in ["Overnight floor", "Electrification modeling", "Self-consumption", "Solar health"]:
            assert title in content
        for path in ["/baseline/", "/electrify/", "/selfuse/", "/solarhealth/"]:
            assert f'{path}"' in content

    def test_stale_hardware_reasons_are_fixed(self, db, client):
        resp = client.get("/")
        content = resp.content.decode()
        assert "needs the Eagle 3" not in content
        assert "load-identification models" in content
