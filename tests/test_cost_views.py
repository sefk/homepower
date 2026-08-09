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

    def test_label_when_cycle_starts_in_may(self):
        cycle = [BillPeriod(end_date=date(2025, 5, 13))]
        assert cycle_label(cycle) == "2025–26"

    def test_label_when_truncated_cycle_starts_jan_to_april(self):
        # A dataset that only retains the tail of a cycle can start with a
        # Jan-April period; it still belongs to the cycle that opened the
        # previous May, so the label's start year is one less than the
        # period's own calendar year.
        cycle = [BillPeriod(end_date=date(2026, 1, 14))]
        assert cycle_label(cycle) == "2025–26"

    def test_label_boundary_at_april_vs_may(self):
        assert cycle_label([BillPeriod(end_date=date(2026, 4, 15))]) == "2025–26"
        assert cycle_label([BillPeriod(end_date=date(2026, 5, 14))]) == "2026–27"


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

    def test_dataset_ending_on_the_april_closer_still_reports_outcome(self, db, client):
        # If the newest bill IS the April true-up bill, that cycle is still
        # the last entry `true_up_cycles` returns — completeness must come
        # from its own end_date, not from "a later cycle exists".
        from billing.data import ELECTRIC_BILLS

        for end_date, peak_kwh, offpeak_kwh, net_kwh, nem_charges in ELECTRIC_BILLS[:12]:
            BillPeriod.objects.create(
                end_date=end_date,
                peak_kwh=peak_kwh,
                offpeak_kwh=offpeak_kwh,
                net_kwh=net_kwh,
                nem_charges=nem_charges,
            )
        data = client.get("/trueup/data.json").json()
        assert len(data["cycles"]) == 1
        assert data["cycles"][0]["complete"] is True
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

    def test_dollars_use_the_periods_own_season_not_kwh_alone(self, db, client):
        # PRD: the question is what the window COSTS, not how many kWh it
        # is — equal kWh isn't equal dollars under TOU. Dec 15 2025 is a
        # winter bill: peak_kwh=474, offpeak_kwh=563.
        call_command("seed_bills")
        data = client.get("/peak/data.json").json()
        i = data["labels"].index("2025-12-15")
        assert data["peak_kwh"][i] == pytest.approx(474)
        assert data["offpeak_kwh"][i] == pytest.approx(563)
        assert data["peak_dollars"][i] == pytest.approx(474 * WINTER_PEAK)
        assert data["offpeak_dollars"][i] == pytest.approx(563 * WINTER_OFFPEAK)


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

    def test_offpeak_weighting_is_correct_across_a_spring_forward_day(self, grid_series):
        """The off-peak morning sub-window (midnight-4pm) contains the
        spring-forward transition, so it has only 15 real hours even
        though it spans 16 wall-clock hours. Weighting the two sub-windows'
        coverage by wall-clock duration (same-tzinfo subtraction) would use
        16h/3h instead of the true 15h/3h and misweight the combined
        fraction.
        """
        from catalog.views import _peak_offpeak_wh

        day = date(2026, 3, 8)  # America/Los_Angeles: 2:00am -> 3:00am
        evening_start = la(2026, 3, 8, 21, 0)
        # cover only the (unaffected) evening sub-window, fully
        record_coverage(
            grid_series, evening_start, evening_start + timedelta(hours=3), CoverageSpan.State.LIVE
        )

        _, _, _, offpeak_cov = _peak_offpeak_wh(
            grid_series, day, timezone.get_current_timezone()
        )
        # true weights: 15h morning (uncovered) + 3h evening (covered) = 18h real
        assert offpeak_cov == pytest.approx(10800 / 64800)
        # the wall-clock-duration bug would instead give 10800 / 68400
        assert offpeak_cov != pytest.approx(10800 / 68400)


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
        # 1kWh at August (summer) rates: $0.66 peak, $0.45 off-peak
        assert f"${SUMMER_PEAK:.2f}" in content
        assert f"${SUMMER_OFFPEAK:.2f}" in content
        assert "unknown" in content  # the other 6 days have no coverage at all


class TestCostmapData:
    def test_uncovered_hours_are_null(self, grid_series, client):
        data = client.get("/costmap/data.json").json()
        assert len(data["z"]) == 24
        assert all(v is None for row in data["z"] for v in row)

    def test_partially_covered_hour_is_null_not_undercounted(
        self, grid_series, client, monkeypatch
    ):
        # Only the first half of the hour has samples/coverage. Without a
        # coverage floor this would render as a real — and misleadingly
        # cheap — dollar figure instead of the outage it actually is.
        frozen_today = date(2026, 7, 15)
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: frozen_today)

        hour_start = la(2026, 7, 15, 10, 0)
        Sample.objects.create(series=grid_series, ts=hour_start, duration_s=1800, value=2000.0)
        record_coverage(
            grid_series, hour_start, hour_start + timedelta(minutes=30), CoverageSpan.State.LIVE
        )

        data = client.get("/costmap/data.json").json()
        di = data["days"].index("2026-07-15")
        assert data["z"][10][di] is None

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


class TestCostmapDst:
    """DST transition days: documented behavior, locked in.

    Spring-forward 2am doesn't exist — zero-width window, coverage 0.0,
    below the floor, null. Fall-back 1am spans both passes; the cell is a
    dollar *total*, so charging both real hours to the wall-clock hour is
    truthful (footer says so).
    """

    def test_spring_forward_2am_is_null(self, grid_series, client, monkeypatch):
        frozen_today = date(2026, 3, 8)  # spring-forward date
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: frozen_today)
        # cover the whole day generously so only the phantom hour can be null
        day_start = la(2026, 3, 8, 0, 0)
        record_coverage(
            grid_series, day_start, day_start + timedelta(hours=26), CoverageSpan.State.LIVE
        )
        Sample.objects.create(series=grid_series, ts=day_start, duration_s=3600, value=1000.0)

        data = client.get("/costmap/data.json").json()
        di = data["days"].index("2026-03-08")
        assert data["z"][2][di] is None  # nonexistent hour, never $0.00

    def test_fall_back_1am_totals_both_passes(self, grid_series, client, monkeypatch):
        from zoneinfo import ZoneInfo

        frozen_today = date(2026, 11, 1)  # fall-back date
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: frozen_today)
        tz = ZoneInfo("America/Los_Angeles")
        first = datetime(2026, 11, 1, 1, 0, tzinfo=tz, fold=0)   # PDT pass
        second = datetime(2026, 11, 1, 1, 0, tzinfo=tz, fold=1)  # PST pass
        for ts in (first, second):
            Sample.objects.create(series=grid_series, ts=ts, duration_s=3600, value=1000.0)
        record_coverage(
            grid_series, first, second + timedelta(hours=1), CoverageSpan.State.LIVE
        )

        data = client.get("/costmap/data.json").json()
        di = data["days"].index("2026-11-01")
        # 2 kWh total across the repeated hour at the winter off-peak rate
        assert data["z"][1][di] == pytest.approx(2.0 * WINTER_OFFPEAK)


class TestCostmapPage:
    def test_renders(self, db, client):
        resp = client.get("/costmap/")
        assert resp.status_code == 200
        assert b"Cost heatmap" in resp.content
        assert b"bill-derived approximations" in resp.content
