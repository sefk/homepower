"""Billing: seeded bill data and TOU rate lookup — cost analyses run
against these until live grid data (Eagle 3) exists.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command

from billing.models import BillPeriod, GasBillPeriod
from billing.rates import (
    PRE_2026_SUMMER_OFFPEAK,
    PRE_2026_SUMMER_PEAK,
    SUMMER_OFFPEAK,
    SUMMER_PEAK,
    WINTER_OFFPEAK,
    WINTER_PEAK,
    rate_for,
    rates_for,
)

from .conftest import utc

LA = ZoneInfo("America/Los_Angeles")


def la(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


class TestSeedBills:
    def test_idempotent(self, db):
        call_command("seed_bills")
        assert BillPeriod.objects.count() == 15
        assert GasBillPeriod.objects.count() == 9

        call_command("seed_bills")  # rerun: same rows, no duplicates
        assert BillPeriod.objects.count() == 15
        assert GasBillPeriod.objects.count() == 9

    def test_true_up_cycle_matches_bill(self, db):
        """The 2025-26 cycle (05/13/2025 through 04/15/2026) must sum to the
        true-up line in the source table, not just seed without error."""
        call_command("seed_bills")
        cycle = BillPeriod.objects.filter(end_date__range=("2025-05-13", "2026-04-15"))
        assert cycle.count() == 12
        assert sum(b.peak_kwh for b in cycle) == pytest.approx(1880)
        assert sum(b.offpeak_kwh for b in cycle) == pytest.approx(-440)
        assert sum(b.net_kwh for b in cycle) == pytest.approx(1439)
        assert sum(b.nem_charges for b in cycle) == pytest.approx(458.26)


class TestRateFor:
    def test_peak_window_start_boundary(self):
        assert rate_for(la(2026, 7, 1, 15, 59)) == SUMMER_OFFPEAK
        assert rate_for(la(2026, 7, 1, 16, 0)) == SUMMER_PEAK

    def test_peak_window_end_boundary(self):
        assert rate_for(la(2026, 7, 1, 20, 59)) == SUMMER_PEAK
        assert rate_for(la(2026, 7, 1, 21, 0)) == SUMMER_OFFPEAK

    def test_summer_starts_june(self):
        assert rate_for(la(2026, 5, 31, 17, 0)) == WINTER_PEAK
        assert rate_for(la(2026, 6, 1, 17, 0)) == SUMMER_PEAK

    def test_summer_ends_september(self):
        assert rate_for(la(2026, 9, 30, 17, 0)) == SUMMER_PEAK
        assert rate_for(la(2026, 10, 1, 17, 0)) == WINTER_PEAK

    def test_aware_utc_datetime_converts_to_local(self):
        # 2026-01-15T00:30Z = Jan 14 16:30 PST -> winter peak
        assert rate_for(utc(2026, 1, 15, 0, 30)) == WINTER_PEAK


class TestRateTables:
    def test_base_services_charge_lowers_2026_summer_rates(self):
        assert rates_for(date(2025, 7, 1)) == (PRE_2026_SUMMER_PEAK, PRE_2026_SUMMER_OFFPEAK)
        assert rates_for(date(2026, 7, 1)) == (SUMMER_PEAK, SUMMER_OFFPEAK)
        assert SUMMER_PEAK < PRE_2026_SUMMER_PEAK
        assert SUMMER_OFFPEAK < PRE_2026_SUMMER_OFFPEAK

    def test_new_table_starts_march_2026(self):
        assert rate_for(la(2026, 2, 28, 23, 59)) == WINTER_OFFPEAK
        assert rate_for(la(2026, 3, 1, 0, 0)) == WINTER_OFFPEAK
        assert rate_for(la(2025, 8, 1, 17, 0)) == PRE_2026_SUMMER_PEAK
        assert rate_for(la(2026, 8, 1, 17, 0)) == SUMMER_PEAK

    def test_summer_2026_rates_reproduce_the_july_bill(self):
        # 06/15-07/14/2026: net 9.9592 peak + 161.0737 off-peak kWh exported.
        # PG&E -$44.79 + WestLight -$11.21, less the -$0.10 franchise fee and
        # WestLight's -$1.71 net generation bonus = $54.19 of energy value.
        value = 9.9592 * SUMMER_PEAK + 161.0737 * SUMMER_OFFPEAK
        assert value == pytest.approx(44.79 + 11.21 - 0.10 - 1.71, abs=0.10)

    def test_peak_always_at_least_offpeak(self):
        # ev.ChargeSession.savings relies on this to never go negative
        for d in (date(2025, 1, 1), date(2025, 7, 1), date(2026, 1, 1), date(2026, 7, 1)):
            peak, offpeak = rates_for(d)
            assert peak >= offpeak
