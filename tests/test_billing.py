"""Billing: seeded bill data and TOU rate lookup — cost analyses run
against these until live grid data (Eagle 3) exists.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command

from billing.models import BillPeriod, CcaAdjustment, GasBillPeriod, UtilityRate
from billing.rates import (
    missing_adjustments,
    parts_for,
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


@pytest.mark.django_db
class TestRateFor:
    def test_peak_window_start_boundary(self):
        peak, offpeak = rates_for(date(2026, 7, 1))
        assert rate_for(la(2026, 7, 1, 15, 59)) == offpeak
        assert rate_for(la(2026, 7, 1, 16, 0)) == peak

    def test_peak_window_end_boundary(self):
        peak, offpeak = rates_for(date(2026, 7, 1))
        assert rate_for(la(2026, 7, 1, 20, 59)) == peak
        assert rate_for(la(2026, 7, 1, 21, 0)) == offpeak

    def test_summer_starts_june(self):
        assert rate_for(la(2026, 5, 31, 17, 0)) == rates_for(date(2026, 5, 31))[0]
        assert rate_for(la(2026, 6, 1, 17, 0)) == rates_for(date(2026, 6, 1))[0]
        assert rates_for(date(2026, 5, 31)) != rates_for(date(2026, 6, 1))

    def test_summer_ends_september(self):
        assert rate_for(la(2026, 9, 30, 17, 0)) == rates_for(date(2026, 9, 30))[0]
        assert rate_for(la(2026, 10, 1, 17, 0)) == rates_for(date(2026, 10, 1))[0]
        assert rates_for(date(2026, 9, 30)) != rates_for(date(2026, 10, 1))

    def test_aware_utc_datetime_converts_to_local(self):
        # 2026-01-15T00:30Z = Jan 14 16:30 PST -> winter peak
        assert rate_for(utc(2026, 1, 15, 0, 30)) == rates_for(date(2026, 1, 14))[0]


@pytest.mark.django_db
class TestRateTables:
    def test_seeded_pre_2026_history_is_all_in(self):
        assert rates_for(date(2025, 7, 1)) == (0.66, 0.45)
        assert rates_for(date(2025, 12, 15)) == (0.625, 0.571)

    def test_base_services_charge_lowers_2026_summer_rates(self):
        before, after = rates_for(date(2025, 7, 1)), rates_for(date(2026, 7, 1))
        assert after[0] < before[0]
        assert after[1] < before[1]

    def test_new_era_starts_march_2026(self):
        assert rates_for(date(2026, 2, 28)) == (0.625, 0.571)
        assert rates_for(date(2026, 3, 1)) != (0.625, 0.571)
        assert rates_for(date(2026, 8, 1)) == rates_for(date(2026, 7, 1))

    def test_summer_2026_rates_reproduce_the_july_bill(self):
        # 06/15-07/14/2026: net 9.9592 peak + 161.0737 off-peak kWh exported,
        # split by WestLight's 7/1 rate change into 5.3116/85.906 kWh before
        # and 4.6476/75.1677 after. PG&E -$44.79 + WestLight -$11.21, less
        # the -$0.10 franchise fee and WestLight's -$1.71 net generation
        # bonus = $54.19 of energy value.
        june_peak, june_offpeak = rates_for(date(2026, 6, 30))
        july_peak, july_offpeak = rates_for(date(2026, 7, 1))
        value = (5.3116 * june_peak + 85.906 * june_offpeak
                 + 4.6476 * july_peak + 75.1677 * july_offpeak)
        assert value == pytest.approx(44.79 + 11.21 - 0.10 - 1.71, abs=0.01)

    def test_westlight_rate_rises_july_2026(self):
        june, july = rates_for(date(2026, 6, 30)), rates_for(date(2026, 7, 1))
        assert july[0] - june[0] == pytest.approx(0.15036 - 0.14048)
        assert july[1] - june[1] == pytest.approx(0.05251 - 0.04778)

    def test_peak_always_at_least_offpeak(self):
        # ev.ChargeSession.savings relies on this to never go negative
        for d in (date(2025, 1, 1), date(2025, 7, 1), date(2026, 1, 1), date(2026, 7, 1), date(2026, 10, 6)):
            peak, offpeak = rates_for(d)
            assert peak >= offpeak


@pytest.mark.django_db
class TestRatePlusAdjustment:
    def test_all_in_is_pge_rate_plus_adjustment(self):
        parts = parts_for(date(2026, 7, 1), "peak")
        assert parts.pge == 0.441
        assert parts.adjustment == pytest.approx(0.5275 - 0.441, abs=2e-4)
        assert parts.all_in == pytest.approx(0.5275, abs=2e-4)
        assert rates_for(date(2026, 7, 1))[0] == parts.all_in

    def test_latest_row_on_or_before_the_date_wins(self):
        UtilityRate.objects.create(
            season="summer", period="peak", tier=1, effective_from=date(2027, 6, 1), rate=0.5, source="opower"
        )
        CcaAdjustment.objects.create(
            season="summer", period="peak", effective_from=date(2027, 6, 15), amount=0.1
        )
        old_adjustment = parts_for(date(2026, 7, 1), "peak").adjustment
        assert parts_for(date(2027, 6, 1), "peak").pge == 0.5
        assert parts_for(date(2027, 6, 14), "peak").all_in == pytest.approx(0.5 + old_adjustment)
        assert parts_for(date(2027, 6, 15), "peak").all_in == pytest.approx(0.6)

    def test_winter_prefers_tier_2_when_one_exists(self):
        assert parts_for(date(2026, 12, 1), "peak").tier == 1
        UtilityRate.objects.create(
            season="winter", period="peak", tier=2, effective_from=date(2026, 11, 1), rate=0.4, source="opower"
        )
        assert parts_for(date(2026, 10, 31), "peak").tier == 1
        assert parts_for(date(2026, 11, 1), "peak").tier == 2
        assert parts_for(date(2026, 11, 1), "peak").pge == 0.4
        assert parts_for(date(2026, 11, 1), "offpeak").tier == 1

    def test_summer_ignores_tier_2(self):
        UtilityRate.objects.create(
            season="summer", period="peak", tier=2, effective_from=date(2026, 3, 1), rate=0.9, source="opower"
        )
        assert parts_for(date(2026, 7, 1), "peak").tier == 1

    def test_saving_a_rate_invalidates_the_cache(self):
        before = rates_for(date(2026, 7, 1))
        UtilityRate.objects.create(
            season="summer", period="peak", tier=1, effective_from=date(2026, 7, 1), rate=0.9, source="bill"
        )
        assert rates_for(date(2026, 7, 1))[0] != before[0]
        assert rates_for(date(2026, 7, 1))[1] == before[1]


@pytest.mark.django_db
class TestMissingAdjustments:
    def test_winter_flagged_on_seed_data(self):
        assert missing_adjustments(date(2026, 10, 6)) == [("winter", "peak"), ("winter", "offpeak")]
        assert missing_adjustments(date(2026, 7, 1)) == [("winter", "peak"), ("winter", "offpeak")]

    def test_before_the_new_era_nothing_is_missing(self):
        assert missing_adjustments(date(2026, 2, 1)) == []

    def test_entering_an_adjustment_clears_that_period_only(self):
        CcaAdjustment.objects.create(
            season="winter", period="peak", effective_from=date(2026, 3, 1), amount=0.07
        )
        assert missing_adjustments(date(2026, 10, 6)) == [("winter", "offpeak")]

    def test_winter_without_adjustment_is_priced_on_pge_side_only(self):
        parts = parts_for(date(2026, 10, 6), "peak")
        assert parts.adjustment == 0.0
        assert parts.all_in == parts.pge


@pytest.mark.django_db
class TestSetCcaAdjustment:
    def test_enters_and_revises_an_adjustment(self):
        call_command("set_cca_adjustment", "winter", "peak", "2026-10-01", "0.0816", "--note", "Nov bill")
        row = CcaAdjustment.objects.get(season="winter", period="peak", effective_from=date(2026, 10, 1))
        assert row.amount == 0.0816 and row.note == "Nov bill"
        call_command("set_cca_adjustment", "winter", "peak", "2026-10-01", "0.09")
        assert CcaAdjustment.objects.filter(season="winter", period="peak").count() == 2  # seed + this
        assert CcaAdjustment.objects.get(effective_from=date(2026, 10, 1), period="peak", season="winter").amount == 0.09
        assert rates_for(date(2026, 10, 6))[0] == pytest.approx(0.3162 + 0.09)

    def test_negative_amounts_are_allowed(self):
        call_command("set_cca_adjustment", "summer", "offpeak", "2026-10-01", "-0.0137")
        assert CcaAdjustment.objects.get(effective_from=date(2026, 10, 1)).amount == -0.0137
