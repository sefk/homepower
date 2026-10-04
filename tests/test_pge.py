"""PG&E gas collector: Opower reads -> gas_wh, re-auth failure, and
agreement with the Green Button importer's write path."""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from opower import MeterType, MfaChallenge

from collectors import pge
from collectors.pge import PgeGasCollector
from core.models import CollectorRun, CoverageSpan, Sample, Source

from .conftest import utc

LA = ZoneInfo("America/Los_Angeles")


def day(y, m, d, therms):
    start = datetime(y, m, d, tzinfo=LA)
    return SimpleNamespace(start_time=start, end_time=start + timedelta(days=1), consumption=therms)


class FakeOpower:
    """Stands in for opower.Opower; class attrs steer each test."""

    mfa = False
    accounts = [SimpleNamespace(meter_type=MeterType.ELEC), SimpleNamespace(meter_type=MeterType.GAS)]
    reads = [day(2026, 9, 28, 1.05), day(2026, 9, 29, 0.0)]
    seen_login_data = None

    def __init__(self, session, utility, username, password, login_data=None):
        assert utility == "pge"
        FakeOpower.seen_login_data = login_data

    async def async_login(self):
        if FakeOpower.mfa:
            raise MfaChallenge("PG&E MFA required", handler=None)

    async def async_get_accounts(self):
        return FakeOpower.accounts

    async def async_get_usage_reads(self, account, aggregate_type, start, end):
        assert account.meter_type == MeterType.GAS
        return FakeOpower.reads


@pytest.fixture
def fake_opower(monkeypatch):
    monkeypatch.setattr(pge, "Opower", FakeOpower)
    FakeOpower.mfa = False
    yield FakeOpower


@pytest.fixture
def collector(tmp_path):
    login = tmp_path / "pge_login.json"
    login.write_text(json.dumps({"validationCookie": "abc"}))
    return PgeGasCollector("user", "pw", login)


class TestPoll:
    def test_gas_reads_become_daily_wh(self, fake_opower, collector):
        readings = asyncio.run(collector.poll())
        assert fake_opower.seen_login_data == {"validationCookie": "abc"}
        assert [r.metric for r in readings] == ["gas_wh", "gas_wh"]
        assert readings[0].ts == utc(2026, 9, 28, 7, 0)
        assert readings[0].duration_s == 86400
        assert readings[0].value == pytest.approx(1.05 * 29307.1)
        assert readings[1].value == 0.0

    def test_trailing_zero_days_are_kept(self, fake_opower, collector, monkeypatch):
        """A run of zero-gas days at the end of the window is real use
        (none), not unposted data -- it must come through as 0, not vanish."""
        monkeypatch.setattr(
            FakeOpower, "reads", [day(2026, 9, 7, 1.06)] + [day(2026, 9, d, 0.0) for d in range(8, 31)]
        )
        readings = asyncio.run(collector.poll())
        assert len(readings) == 24
        assert readings[-1].ts == utc(2026, 9, 30, 7, 0)
        assert readings[-1].value == 0.0

    def test_mfa_means_rerun_pge_auth(self, fake_opower, collector):
        fake_opower.mfa = True
        with pytest.raises(RuntimeError, match="pge_auth"):
            asyncio.run(collector.poll())

    def test_missing_login_file_still_tries(self, fake_opower, tmp_path):
        asyncio.run(PgeGasCollector("u", "p", tmp_path / "absent.json").poll())
        assert fake_opower.seen_login_data == {}


class TestRunOnce:
    def test_writes_backfilled_gas(self, fake_opower, collector, transactional_db):
        asyncio.run(collector.setup())
        assert asyncio.run(collector.run_once()) is True

        assert Source.objects.get(slug="pge_gas").kind == Source.Kind.GAS
        assert Sample.objects.filter(series__metric="gas_wh").count() == 2
        span = CoverageSpan.objects.get(series__metric="gas_wh")
        assert span.state == CoverageSpan.State.BACKFILLED
        assert (span.start, span.end) == (utc(2026, 9, 28, 7, 0), utc(2026, 9, 30, 7, 0))

    def test_repoll_is_idempotent(self, fake_opower, collector, transactional_db):
        asyncio.run(collector.setup())
        asyncio.run(collector.run_once())
        asyncio.run(collector.run_once())
        assert Sample.objects.filter(series__metric="gas_wh").count() == 2

    def test_reauth_shows_as_failed_run(self, fake_opower, collector, transactional_db):
        asyncio.run(collector.setup())
        fake_opower.mfa = True
        assert asyncio.run(collector.run_once()) is False
        assert "pge_auth" in CollectorRun.objects.get().message

    def test_agrees_with_green_button_import(self, fake_opower, collector, tmp_path, transactional_db):
        """Same day from a hand import and a poll: one sample, same value."""
        path = tmp_path / "gas.csv"
        path.write_text(
            "TYPE,DATE,START TIME,END TIME,USAGE (therms),COST,NOTES\n"
            "Natural gas usage,2026-09-28,00:00,23:59,1.05,$2.79\n"
        )
        call_command("import_greenbutton", str(path))
        asyncio.run(collector.setup())
        asyncio.run(collector.run_once())
        samples = Sample.objects.filter(series__metric="gas_wh", ts=utc(2026, 9, 28, 7, 0))
        assert samples.count() == 1
        assert samples.get().value == pytest.approx(1.05 * 29307.1)
