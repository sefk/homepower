"""pge_rates: deriving PG&E's TOU rates from Opower cost reads, detecting
rate changes, and writing them. Opower is faked; no network."""

import asyncio
import json
from datetime import date, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from opower import MeterType, MfaChallenge

from billing import rates
from billing.models import UtilityRate
from collectors import pge_rates
from collectors.pge_rates import (
    PgeRatesCollector,
    derive_daily_rates,
    plan_changes,
    rate_eras,
)
from core.models import CollectorRun

LA = ZoneInfo("America/Los_Angeles")


def comp(season, day_part, tier, rate, kwh):
    return SimpleNamespace(
        season=season, day_part=day_part, tier_number=tier, cost=rate * kwh, consumption=kwh, tier_type="ORDINAL"
    )


def hour(y, m, d, h, *components):
    start = datetime(y, m, d, h, tzinfo=LA)
    return SimpleNamespace(start_time=start, end_time=start + timedelta(hours=1), read_components=list(components))


def day_reads(d: date, season, peak_rate, off_rate, tier=1, kwh=1.0):
    """A full local day: peak 4-9pm, off-peak the rest."""
    out = []
    for h in range(24):
        if 16 <= h < 21:
            out.append(hour(d.year, d.month, d.day, h, comp(season, "ON_PEAK+RT02/TOD", tier, peak_rate, kwh)))
        else:
            out.append(hour(d.year, d.month, d.day, h, comp(season, "OFF_PEAK+RT02/TOD", tier, off_rate, kwh)))
    return out


def days(start: date, n: int, *args, **kw):
    reads = []
    for i in range(n):
        reads += day_reads(start + timedelta(days=i), *args, **kw)
    return reads


WINTER_T1 = ("winter", "peak", 1)


class TestDerive:
    def test_rate_is_cost_over_consumption_per_local_day(self):
        daily = derive_daily_rates(day_reads(date(2026, 10, 5), "WINTER", 0.3162, 0.2862))
        assert daily[date(2026, 10, 5)] == {
            ("winter", "peak", 1): pytest.approx(0.3162),
            ("winter", "offpeak", 1): pytest.approx(0.2862),
        }

    def test_export_hours_at_the_same_rate_agree(self):
        reads = [
            hour(2026, 7, 1, 17, comp("SUMMER", "ON_PEAK", 1, 0.441, -2.0)),
            hour(2026, 7, 1, 18, comp("SUMMER", "ON_PEAK", 1, 0.441, 0.5)),
        ]
        assert derive_daily_rates(reads)[date(2026, 7, 1)][("summer", "peak", 1)] == pytest.approx(0.441)

    def test_balanced_day_uses_gross_energy_not_net(self):
        # Import and export cancel exactly: net is 0 kWh / $0 (no ratio at
        # all, and any cost noise would dominate a near-zero one); gross
        # still carries the rate.
        reads = [
            hour(2026, 7, 1, 17, comp("SUMMER", "ON_PEAK", 1, 0.441, 3.0)),
            hour(2026, 7, 1, 18, comp("SUMMER", "ON_PEAK", 1, 0.441, -3.0)),
        ]
        rate = derive_daily_rates(reads)[date(2026, 7, 1)][("summer", "peak", 1)]
        assert rate == pytest.approx(0.441)

    def test_tiny_consumption_is_skipped_not_zero(self):
        reads = [hour(2026, 7, 1, 17, comp("SUMMER", "ON_PEAK", 1, 0.441, 0.004))]
        assert derive_daily_rates(reads) == {}

    def test_tiers_are_separate(self):
        reads = [
            hour(2026, 11, 3, 10, comp("WINTER", "OFF_PEAK", 1, 0.2862, 1.0), comp("WINTER", "OFF_PEAK", 2, 0.37, 1.0)),
        ]
        assert derive_daily_rates(reads)[date(2026, 11, 3)] == {
            ("winter", "offpeak", 1): pytest.approx(0.2862),
            ("winter", "offpeak", 2): pytest.approx(0.37),
        }

    def test_hours_group_by_local_day_not_utc(self):
        # 23:00 PDT on Jul 1 is Jul 2 06:00 UTC
        reads = [hour(2026, 7, 1, 23, comp("SUMMER", "OFF_PEAK", 1, 0.318, 1.0))]
        assert list(derive_daily_rates(reads)) == [date(2026, 7, 1)]

    def test_components_without_season_or_period_are_ignored(self):
        reads = [hour(2026, 7, 1, 10, comp(None, None, None, 0.3, 1.0))]
        assert derive_daily_rates(reads) == {}

    def test_missing_days_stay_absent(self):
        reads = day_reads(date(2026, 10, 1), "WINTER", 0.3, 0.2) + day_reads(date(2026, 10, 4), "WINTER", 0.3, 0.2)
        assert sorted(derive_daily_rates(reads)) == [date(2026, 10, 1), date(2026, 10, 4)]


class TestEras:
    def test_season_switch_gives_separate_keys(self):
        reads = day_reads(date(2026, 9, 30), "SUMMER", 0.441, 0.318) + day_reads(date(2026, 10, 1), "WINTER", 0.3162, 0.2862)
        eras = rate_eras(derive_daily_rates(reads))
        assert eras[("summer", "peak", 1)] == [(date(2026, 9, 30), date(2026, 9, 30), pytest.approx(0.441))]
        assert eras[WINTER_T1] == [(date(2026, 10, 1), date(2026, 10, 1), pytest.approx(0.3162))]

    def test_a_rate_change_starts_a_new_era_on_its_first_day(self):
        reads = days(date(2026, 11, 1), 3, "WINTER", 0.3162, 0.2862) + days(date(2026, 11, 4), 2, "WINTER", 0.35, 0.30)
        eras = rate_eras(derive_daily_rates(reads))[WINTER_T1]
        assert [(a, b) for a, b, _ in eras] == [
            (date(2026, 11, 1), date(2026, 11, 3)),
            (date(2026, 11, 4), date(2026, 11, 5)),
        ]

    def test_days_before_the_opower_basis_are_ignored(self):
        reads = day_reads(date(2026, 2, 28), "WINTER", 0.9, 0.9) + day_reads(date(2026, 3, 1), "WINTER", 0.3, 0.2)
        eras = rate_eras(derive_daily_rates(reads))[WINTER_T1]
        assert [a for a, _, _ in eras] == [date(2026, 3, 1)]


def rows(*items):
    """existing-rows argument: key -> [(effective_from, rate, source)]"""
    out = {}
    for key, eff, rate, source in items:
        out.setdefault(key, []).append((eff, rate, source))
    return out


class TestPlanChanges:
    EXISTING = rows((WINTER_T1, date(2026, 3, 1), 0.3162, "opower"))

    def test_matching_rate_is_no_change(self):
        daily = derive_daily_rates(days(date(2026, 10, 1), 5, "WINTER", 0.3162, 0.2862))
        assert [c for c in plan_changes(daily, self.EXISTING) if c.key == WINTER_T1] == []

    def test_change_is_effective_the_first_day_it_appears(self):
        daily = derive_daily_rates(
            days(date(2026, 10, 1), 3, "WINTER", 0.3162, 0.2862) + days(date(2026, 10, 4), 4, "WINTER", 0.34, 0.2862)
        )
        [change] = [c for c in plan_changes(daily, self.EXISTING) if c.key == WINTER_T1]
        assert change.effective_from == date(2026, 10, 4)
        assert change.rate == pytest.approx(0.34)
        assert change.previous == 0.3162
        assert not change.update

    def test_difference_within_threshold_is_rounding(self):
        daily = derive_daily_rates(days(date(2026, 10, 1), 3, "WINTER", 0.3162 + 0.0004, 0.2862))
        assert [c for c in plan_changes(daily, self.EXISTING) if c.key == WINTER_T1] == []

    def test_difference_over_threshold_is_a_change(self):
        daily = derive_daily_rates(days(date(2026, 10, 1), 1, "WINTER", 0.3162 + 0.0006, 0.2862))
        assert len([c for c in plan_changes(daily, self.EXISTING) if c.key == WINTER_T1]) == 1

    def test_provisional_opower_row_on_the_same_day_is_revised(self):
        daily = derive_daily_rates(days(date(2026, 3, 1), 3, "WINTER", 0.30, 0.2862))
        [change] = [c for c in plan_changes(daily, self.EXISTING) if c.key == WINTER_T1]
        assert change.update and change.effective_from == date(2026, 3, 1)

    def test_bill_entered_row_is_never_overwritten(self):
        existing = rows((WINTER_T1, date(2026, 3, 1), 0.3162, "bill"))
        daily = derive_daily_rates(days(date(2026, 3, 1), 3, "WINTER", 0.30, 0.2862))
        assert [c for c in plan_changes(daily, existing) if c.key == WINTER_T1] == []

    def test_tiers_without_a_row_open_one(self):
        daily = derive_daily_rates([hour(2026, 11, 3, 10, comp("WINTER", "OFF_PEAK", 2, 0.37, 1.0))])
        [change] = plan_changes(daily, {})
        assert change.key == ("winter", "offpeak", 2) and change.previous is None

    def test_plan_is_stable_when_reapplied(self):
        daily = derive_daily_rates(
            days(date(2026, 10, 1), 3, "WINTER", 0.3162, 0.2862) + days(date(2026, 10, 4), 4, "WINTER", 0.34, 0.31)
        )
        first = plan_changes(daily, self.EXISTING)
        applied = {k: list(v) for k, v in self.EXISTING.items()}
        for c in first:
            applied.setdefault(c.key, []).append((c.effective_from, c.rate, "opower"))
        assert plan_changes(daily, applied) == []

    def test_gap_in_days_is_not_a_change(self):
        daily = derive_daily_rates(
            day_reads(date(2026, 10, 1), "WINTER", 0.3162, 0.2862) + day_reads(date(2026, 10, 9), "WINTER", 0.3162, 0.2862)
        )
        assert [c for c in plan_changes(daily, self.EXISTING) if c.key == WINTER_T1] == []


class FakeOpower:
    mfa = False
    reads: list = []
    windows: list = []

    def __init__(self, session, utility, username, password, login_data=None):
        assert utility == "pge"

    async def async_login(self):
        if FakeOpower.mfa:
            raise MfaChallenge("PG&E MFA required", handler=None)

    async def async_get_accounts(self):
        return [SimpleNamespace(meter_type=MeterType.GAS), SimpleNamespace(meter_type=MeterType.ELEC)]

    async def async_get_cost_reads(self, account, aggregate_type, start, end):
        assert account.meter_type == MeterType.ELEC
        FakeOpower.windows.append((start, end))
        return [r for r in FakeOpower.reads if start <= r.start_time < end]


@pytest.fixture
def fake_opower(monkeypatch):
    monkeypatch.setattr(pge_rates, "Opower", FakeOpower)
    FakeOpower.mfa = False
    FakeOpower.windows = []
    FakeOpower.reads = []
    return FakeOpower


@pytest.fixture
def collector(tmp_path):
    login = tmp_path / "login.json"
    login.write_text(json.dumps({"validationCookie": "abc"}))
    return PgeRatesCollector("u", "p", login)


class TestCollector:
    def test_new_rate_is_recorded_and_surfaced_in_the_run_message(self, fake_opower, collector, transactional_db):
        today = datetime.now(LA).date()
        fake_opower.reads = days(today - timedelta(days=6), 7, "WINTER", 0.3162, 0.2862)
        # PG&E's winter rate moves 3 days ago
        fake_opower.reads = days(today - timedelta(days=6), 3, "WINTER", 0.3162, 0.2862) + days(
            today - timedelta(days=3), 3, "WINTER", 0.36, 0.2862
        )
        asyncio.run(collector.setup())
        assert asyncio.run(collector.run_once()) is True
        row = UtilityRate.objects.get(season="winter", period="peak", tier=1, effective_from=today - timedelta(days=3))
        assert row.rate == pytest.approx(0.36) and row.source == "opower"
        assert rates.rates_for(today)[0] != 0  # lookup sees the row after the cache flush
        run = CollectorRun.objects.get()
        assert run.ok and "RATE CHANGE" in run.message and "0.3162 -> 0.3600" in run.message

    def test_rerun_is_idempotent(self, fake_opower, collector, transactional_db):
        today = datetime.now(LA).date()
        fake_opower.reads = days(today - timedelta(days=5), 3, "WINTER", 0.3162, 0.2862) + days(
            today - timedelta(days=2), 2, "WINTER", 0.36, 0.2862
        )
        asyncio.run(collector.setup())
        asyncio.run(collector.run_once())
        n = UtilityRate.objects.count()
        asyncio.run(collector.run_once())
        assert UtilityRate.objects.count() == n
        assert "RATE CHANGE" not in CollectorRun.objects.order_by("-started").first().message

    def test_no_data_changes_nothing(self, fake_opower, collector, transactional_db):
        n = UtilityRate.objects.count()
        asyncio.run(collector.setup())
        assert asyncio.run(collector.run_once()) is True
        assert UtilityRate.objects.count() == n

    def test_mfa_fails_the_run_visibly(self, fake_opower, collector, transactional_db):
        fake_opower.mfa = True
        asyncio.run(collector.setup())
        assert asyncio.run(collector.run_once()) is False
        run = CollectorRun.objects.get()
        assert not run.ok and "pge_auth" in run.message

    def test_requests_are_chunked_by_month(self, fake_opower, collector):
        end = datetime(2026, 10, 6, tzinfo=LA)
        asyncio.run(
            pge_rates.fetch_cost_reads("u", "p", collector.login_file, end - timedelta(days=100), end)
        )
        assert len(fake_opower.windows) == 4
        assert all(b - a <= timedelta(days=pge_rates.CHUNK_DAYS) for a, b in fake_opower.windows)
        assert fake_opower.windows[0][0] == end - timedelta(days=100) and fake_opower.windows[-1][1] == end


class TestBackfillCommand:
    @pytest.fixture
    def history(self, fake_opower):
        fake_opower.reads = days(date(2026, 9, 25), 3, "SUMMER", 0.441, 0.318) + days(
            date(2026, 10, 1), 3, "WINTER", 0.3162, 0.2862
        ) + days(date(2026, 10, 4), 2, "WINTER", 0.40, 0.2862)

    def run(self, *args, settings):
        settings.PGE_USERNAME, settings.PGE_PASSWORD = "u", "p"
        out = StringIO()
        call_command("pge_rates_backfill", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_prints_eras_and_writes_nothing(self, history, settings, db):
        n = UtilityRate.objects.count()
        out = self.run("--days", "400", "--dry-run", settings=settings)
        assert "winter peak    tier 1  2026-10-01 .. 2026-10-03  0.3162" in out
        assert "winter peak    tier 1  2026-10-04 .. 2026-10-05  0.4000" in out
        assert "0.3162 -> 0.4000 from 2026-10-04" in out
        assert UtilityRate.objects.count() == n

    def test_writes_without_dry_run(self, history, settings, db):
        self.run("--days", "400", settings=settings)
        row = UtilityRate.objects.get(season="winter", period="peak", tier=1, effective_from=date(2026, 10, 4))
        assert row.rate == pytest.approx(0.40)


class TestHealthPage:
    def test_shows_missing_adjustment_and_last_rate_change(self, client, no_winter_adjustment):
        UtilityRate.objects.create(
            season="winter", period="peak", tier=1, effective_from=date(2026, 10, 4), rate=0.4, source="opower"
        )
        content = client.get("/health/").content.decode()
        assert "CCA adjustment not entered for winter peak, winter off-peak" in content
        assert "Last PG&amp;E rate change: winter" in content
        assert "$0.4000" in content
