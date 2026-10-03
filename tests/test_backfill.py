"""Backfill: storage rules, Enlighten parsing, and the command's chunking."""

import asyncio
from datetime import date, timedelta

import pytest

from collectors.backfill import store_backfill
from collectors.base import Reading
from collectors.enlighten import parse_daily_energy
from core import coverage
from core.models import CoverageSpan, Sample

from .conftest import utc

LIVE = CoverageSpan.State.LIVE
BACKFILLED = CoverageSpan.State.BACKFILLED


def quarter(ts, value=400.0):
    return Reading(metric="production_w", unit="W", ts=ts, duration_s=900, value=value)


class TestStoreBackfill:
    def test_writes_samples_and_one_span_per_contiguous_run(self, series):
        readings = [
            quarter(utc(2026, 7, 1, 10, 0)),
            quarter(utc(2026, 7, 1, 10, 15)),
            # 10:30 missing at the vendor: must stay unknown, not zero
            quarter(utc(2026, 7, 1, 10, 45)),
        ]
        result = store_backfill(series.source, readings)
        assert result.written == 3
        assert Sample.objects.count() == 3
        spans = list(CoverageSpan.objects.order_by("start"))
        assert [(s.start, s.end, s.state) for s in spans] == [
            (utc(2026, 7, 1, 10, 0), utc(2026, 7, 1, 10, 30), BACKFILLED),
            (utc(2026, 7, 1, 10, 45), utc(2026, 7, 1, 11, 0), BACKFILLED),
        ]
        assert coverage.uncovered(series, utc(2026, 7, 1, 10, 0), utc(2026, 7, 1, 11, 0)) == [
            (utc(2026, 7, 1, 10, 30), utc(2026, 7, 1, 10, 45))
        ]

    def test_rerun_is_idempotent_and_takes_corrected_values(self, series):
        store_backfill(series.source, [quarter(utc(2026, 7, 1, 10, 0), 400.0)])
        store_backfill(series.source, [quarter(utc(2026, 7, 1, 10, 0), 450.0)])
        assert Sample.objects.get().value == 450.0
        assert CoverageSpan.objects.count() == 1

    def test_live_coverage_wins(self, series):
        """Minute-level live samples already cover 10:10-10:20. A coarser
        vendor quarter overlapping that would double-count on integration,
        so both quarters touching it are dropped; the clear one is kept."""
        Sample.objects.create(series=series, ts=utc(2026, 7, 1, 10, 10), duration_s=60, value=500)
        coverage.record_coverage(series, utc(2026, 7, 1, 10, 10), utc(2026, 7, 1, 10, 20), LIVE)
        result = store_backfill(
            series.source,
            [
                quarter(utc(2026, 7, 1, 10, 0)),  # overlaps live 10:10-10:15
                quarter(utc(2026, 7, 1, 10, 15)),  # overlaps live 10:15-10:20
                quarter(utc(2026, 7, 1, 10, 30)),
            ],
        )
        assert (result.written, result.skipped_live) == (1, 2)
        assert Sample.objects.filter(duration_s=900).count() == 1
        backfilled = CoverageSpan.objects.get(state=BACKFILLED)
        assert (backfilled.start, backfilled.end) == (
            utc(2026, 7, 1, 10, 30),
            utc(2026, 7, 1, 10, 45),
        )

    def test_quarter_touching_live_edge_is_kept(self, series):
        coverage.record_coverage(series, utc(2026, 7, 1, 10, 15), utc(2026, 7, 1, 11, 0), LIVE)
        result = store_backfill(series.source, [quarter(utc(2026, 7, 1, 10, 0))])
        assert result.written == 1  # half-open: [10:00, 10:15) does not overlap

    def test_empty_batch_is_a_no_op(self, series):
        result = store_backfill(series.source, [])
        assert result.written == 0
        assert CoverageSpan.objects.count() == 0


def enlighten_day(start_epoch, production, interval=900):
    return {"start_time": start_epoch, "interval_length": interval, "production": production}


class TestParseDailyEnergy:
    START = int(utc(2026, 9, 1, 7, 0).timestamp())  # local midnight, PDT

    def test_quarter_wh_becomes_mean_watts(self):
        payload = {
            "last_report_date": self.START + 86400,
            "stats": [enlighten_day(self.START, [0, 25, 100])],
        }
        readings = parse_daily_energy(payload)
        assert [r.value for r in readings] == [0.0, 100.0, 400.0]  # Wh per quarter x 4
        assert readings[1].ts == utc(2026, 9, 1, 7, 15)
        assert readings[1].duration_s == 900
        assert readings[1].metric == "production_w"

    def test_null_intervals_are_skipped(self):
        payload = {
            "last_report_date": self.START + 86400,
            "stats": [enlighten_day(self.START, [10, None, 30])],
        }
        assert len(parse_daily_energy(payload)) == 2

    def test_intervals_after_last_report_are_not_known_zeros(self):
        """Enlighten zero-fills the rest of today. Those are placeholders,
        not production of zero — only intervals the Envoy has reported
        through may be stored."""
        payload = {
            "last_report_date": self.START + 2 * 900,
            "stats": [enlighten_day(self.START, [10, 20, 0, 0])],
        }
        assert [r.value for r in parse_daily_energy(payload)] == [40.0, 80.0]

    def test_fall_back_day_has_100_distinct_quarters(self):
        start = int(utc(2026, 11, 1, 7, 0).timestamp())  # local midnight before fall-back
        payload = {
            "last_report_date": start + 100 * 900,
            "stats": [enlighten_day(start, [1] * 100)],
        }
        readings = parse_daily_energy(payload)
        assert len({r.ts for r in readings}) == 100
        assert readings[-1].ts + timedelta(seconds=900) == utc(2026, 11, 2, 8, 0)


class FakeSolarEdge:
    """Records the windows the command asks for; one reading per window."""

    def __init__(self):
        self.windows = []

    def _ensure_source(self):
        from core.models import Source

        return Source.objects.get(slug="envoy")

    async def installation_date(self, session):
        return date(2026, 7, 1)

    async def fetch_power(self, session, start, end, now=None):
        self.windows.append((start, end))
        return [quarter(utc(start.year, start.month, start.day, 18, 0))]


class TestCommand:
    def test_solaredge_walks_history_in_week_chunks(
        self, transactional_db, series, settings, monkeypatch
    ):
        from collectors.management.commands import backfill as cmd

        fake = FakeSolarEdge()
        settings.SOLAREDGE_USERNAME = "me@example.com"
        settings.SOLAREDGE_PASSWORD = "pw"
        settings.SOLAREDGE_SITE_ID = "1"
        monkeypatch.setattr(cmd, "SolarEdgeCollector", lambda **kw: fake)
        monkeypatch.setattr(cmd, "PAUSE_S", 0)
        command = cmd.Command()

        total = asyncio.run(cmd.backfill_solaredge(command, None, date(2026, 7, 16)))

        assert fake.windows == [
            (date(2026, 7, 1), date(2026, 7, 7)),
            (date(2026, 7, 8), date(2026, 7, 14)),
            (date(2026, 7, 15), date(2026, 7, 16)),  # clipped to --end
        ]
        assert total.written == 3

    def test_unconfigured_source_is_skipped_not_an_error(self, settings, db, capsys):
        from django.core.management import call_command

        settings.SOLAREDGE_USERNAME = ""
        call_command("backfill", "solaredge")
        assert "skipped" in capsys.readouterr().out

    def test_unknown_source_is_rejected(self, db):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="no backfill for eagle"):
            call_command("backfill", "eagle")
