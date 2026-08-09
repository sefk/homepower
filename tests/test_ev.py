"""EV charge session derivation (catalog/ev.py) and the /ev/ view.

Windows are built the same way the rest of the cost views do -- aware
local datetimes, normalized to UTC inside coverage (docs/REVIEW-
INSIGHTS.md) -- and gaps come only from core.coverage, never from
timestamp arithmetic of this test's own.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from billing.rates import SUMMER_OFFPEAK, SUMMER_PEAK
from catalog.ev import sessions
from core.coverage import record_coverage
from core.models import CoverageSpan, Sample, Series, Source

LA = ZoneInfo("America/Los_Angeles")


def la(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


@pytest.fixture
def tesla_series(db) -> Series:
    source = Source.objects.create(
        slug="tesla",
        name="EV (Tesla Model S)",
        kind=Source.Kind.EV,
        poll_interval_s=60,
        native_resolution_s=60,
    )
    return Series.objects.create(source=source, metric="ev_charge_power_w", unit="W")


class TestSessionsSplitByZeroSample:
    def test_zero_sample_breaks_one_run_into_two_sessions(self, tesla_series):
        start = la(2026, 8, 4, 22, 0)
        samples = [
            (start, 3000.0),
            (start + timedelta(minutes=1), 3000.0),
            (start + timedelta(minutes=2), 0.0),  # explicit stop
            (start + timedelta(minutes=3), 3200.0),
            (start + timedelta(minutes=4), 3200.0),
        ]
        for ts, value in samples:
            Sample.objects.create(series=tesla_series, ts=ts, duration_s=60, value=value)
        record_coverage(
            tesla_series, start, start + timedelta(minutes=5), CoverageSpan.State.LIVE
        )

        result = sessions(tesla_series, start - timedelta(hours=1), start + timedelta(hours=1))

        assert len(result) == 2
        # most-recent-first
        assert result[0].start == start + timedelta(minutes=3)
        assert result[1].start == start


class TestSessionsSplitByCoverageGap:
    def test_coverage_gap_breaks_one_run_into_two_sessions(self, tesla_series):
        start = la(2026, 8, 4, 22, 0)
        Sample.objects.create(series=tesla_series, ts=start, duration_s=60, value=3000.0)
        Sample.objects.create(
            series=tesla_series, ts=start + timedelta(minutes=1), duration_s=60, value=3000.0
        )
        record_coverage(tesla_series, start, start + timedelta(minutes=2), CoverageSpan.State.LIVE)

        # a real outage: no coverage at all for 30 minutes, then charging resumes
        resume = start + timedelta(minutes=32)
        Sample.objects.create(series=tesla_series, ts=resume, duration_s=60, value=3200.0)
        Sample.objects.create(
            series=tesla_series, ts=resume + timedelta(minutes=1), duration_s=60, value=3200.0
        )
        record_coverage(tesla_series, resume, resume + timedelta(minutes=2), CoverageSpan.State.LIVE)

        result = sessions(tesla_series, start - timedelta(hours=1), start + timedelta(hours=1))

        assert len(result) == 2
        assert result[0].start == resume
        assert result[1].start == start

    def test_no_gap_bridges_into_one_session(self, tesla_series):
        """Sanity check for the gap test above: fully-covered contiguous
        samples with no zero reading must NOT be split."""
        start = la(2026, 8, 4, 22, 0)
        for i in range(4):
            Sample.objects.create(
                series=tesla_series, ts=start + timedelta(minutes=i), duration_s=60, value=3000.0
            )
        record_coverage(tesla_series, start, start + timedelta(minutes=4), CoverageSpan.State.LIVE)

        result = sessions(tesla_series, start - timedelta(hours=1), start + timedelta(hours=1))
        assert len(result) == 1


class TestSessionCostAcrossPeakBoundary:
    def test_session_crossing_9pm_prices_each_sample_at_its_own_rate(self, tesla_series):
        # 4000W for 900s (15 min) = 1 kWh per sample. Three samples land
        # before 9pm local (peak), one lands at exactly 9pm (off-peak --
        # PEAK_END_HOUR is exclusive).
        start = la(2026, 8, 4, 20, 15)  # summer date
        for i in range(4):
            Sample.objects.create(
                series=tesla_series,
                ts=start + timedelta(minutes=15 * i),
                duration_s=900,
                value=4000.0,
            )
        record_coverage(
            tesla_series, start, start + timedelta(minutes=60), CoverageSpan.State.LIVE
        )

        [session] = sessions(tesla_series, start - timedelta(hours=1), start + timedelta(hours=2))

        assert session.kwh == pytest.approx(4.0)
        expected_actual = 3 * SUMMER_PEAK + 1 * SUMMER_OFFPEAK
        expected_counterfactual = 4 * SUMMER_OFFPEAK
        assert session.actual_cost == pytest.approx(expected_actual)
        assert session.counterfactual_cost == pytest.approx(expected_counterfactual)
        assert session.savings == pytest.approx(expected_actual - expected_counterfactual)
        assert session.savings > 0


class TestEvView:
    def test_empty_state_when_no_tesla_source(self, db, client):
        resp = client.get("/ev/")
        assert resp.status_code == 200
        assert b"No Tesla data yet" in resp.content
        assert b"ops/tesla-setup.md" in resp.content

    def test_no_sessions_yet_shows_distinct_message(self, tesla_series, client):
        resp = client.get("/ev/")
        assert b"No Tesla data yet" not in resp.content
        assert b"No charge sessions" in resp.content

    def test_renders_session_table_with_costs(self, tesla_series, client, monkeypatch):
        start = la(2026, 8, 4, 12, 0)  # off-peak, summer
        monkeypatch.setattr("catalog.views.timezone.now", lambda: start + timedelta(days=1))
        for i in range(2):
            Sample.objects.create(
                series=tesla_series,
                ts=start + timedelta(minutes=i),
                duration_s=60,
                value=7680.0,
            )
        record_coverage(
            tesla_series, start, start + timedelta(minutes=2), CoverageSpan.State.LIVE
        )

        resp = client.get("/ev/")
        content = resp.content.decode()
        assert resp.status_code == 200
        assert "2m" in content  # duration label
        assert "$" in content
