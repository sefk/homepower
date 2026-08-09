"""Collector base: storage, coverage bridging, failure and restart semantics."""

import asyncio
from datetime import timedelta

import pytest

from collectors.base import Collector, Reading
from core.models import CollectorRun, CoverageSpan, Sample, Source

from .conftest import utc


class FakeCollector(Collector):
    slug = "fake"
    name = "Fake source"
    kind = Source.Kind.SOLAR
    poll_interval_s = 60
    native_resolution_s = 60

    def __init__(self, readings_per_poll):
        super().__init__()
        self._polls = list(readings_per_poll)

    async def poll(self):
        batch = self._polls.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch


def reading(ts, value=500.0, metric="production_w"):
    return Reading(metric=metric, unit="W", ts=ts, duration_s=60, value=value)


def run(collector, times=1):
    async def _go():
        await collector.setup()
        for _ in range(times):
            await collector.run_once()

    asyncio.run(_go())


class TestStore:
    def test_poll_writes_samples_and_live_coverage(self, transactional_db):
        t = utc(2026, 8, 1, 10, 0)
        run(FakeCollector([[reading(t)]]))

        sample = Sample.objects.get()
        assert (sample.value, sample.duration_s) == (500.0, 60)
        span = CoverageSpan.objects.get()
        assert span.state == CoverageSpan.State.LIVE
        assert (span.start, span.end) == (t, t + timedelta(seconds=60))

    def test_repolling_same_ts_is_idempotent(self, transactional_db):
        t = utc(2026, 8, 1, 10, 0)
        run(FakeCollector([[reading(t, value=100.0)], [reading(t, value=200.0)]]), times=2)
        sample = Sample.objects.get()  # updated, not duplicated
        assert sample.value == 200.0

    def test_jittered_polls_stay_one_contiguous_span(self, transactional_db):
        """Scheduler jitter must not shred live collection into micro-gaps."""
        t = utc(2026, 8, 1, 10, 0)
        polls = [[reading(t)], [reading(t + timedelta(seconds=61, microseconds=250000))]]
        run(FakeCollector(polls), times=2)

        span = CoverageSpan.objects.get()
        assert span.start == t

    def test_overlapping_reread_window_does_not_invert_coverage(self, transactional_db):
        """A collector that re-reads a lookback window (SolarEdge) hands back
        older timestamps; bridging them backwards used to raise and kill the
        process. Re-reads must merge quietly and never move _last_ts back."""
        t = utc(2026, 8, 1, 10, 0)
        polls = [
            [reading(t), reading(t + timedelta(seconds=60))],
            # second poll re-reads the same quarter-hour plus one new reading
            [reading(t), reading(t + timedelta(seconds=60)), reading(t + timedelta(seconds=120))],
        ]
        run(FakeCollector(polls), times=2)

        span = CoverageSpan.objects.get()  # still one clean span
        assert (span.start, span.end) == (t, t + timedelta(seconds=180))
        assert CollectorRun.objects.filter(ok=False).count() == 0

    def test_store_failure_records_failed_run_not_crash(self, transactional_db):
        """Storage bugs surface on the health page, not as a launchd crash loop."""
        bad = reading(utc(2026, 8, 1, 10, 0))
        bad.duration_s = 0  # forces record_coverage's empty-span ValueError
        collector = FakeCollector([[bad]])

        async def _go():
            await collector.setup()
            return await collector.run_once()

        assert asyncio.run(_go()) is False  # must not raise
        run_row = CollectorRun.objects.get()
        assert run_row.ok is False
        assert "store failed" in run_row.message

    def test_source_row_created_and_updated(self, transactional_db):
        run(FakeCollector([[reading(utc(2026, 8, 1, 10, 0))]]))
        source = Source.objects.get(slug="fake")
        assert source.poll_interval_s == 60


class TestFailure:
    def test_failed_poll_records_run_and_no_coverage(self, transactional_db):
        collector = FakeCollector([RuntimeError("vendor 503")])
        run(collector)

        run_row = CollectorRun.objects.get()
        assert run_row.ok is False
        assert "vendor 503" in run_row.message
        assert CoverageSpan.objects.count() == 0

    def test_failure_between_polls_leaves_a_gap_severed(self, transactional_db):
        """A failed poll drops the bridge only if the outage exceeds grace."""
        t = utc(2026, 8, 1, 10, 0)
        polls = [
            [reading(t)],
            RuntimeError("down"),
            [reading(t + timedelta(minutes=5))],  # beyond 2x interval grace
        ]
        run(FakeCollector(polls), times=3)
        assert CoverageSpan.objects.count() == 2  # outage stays unknown


class TestRestart:
    def test_downtime_across_restart_reads_as_unknown(self, transactional_db):
        """PRD: gap detected on restart — the bridge must not survive the process."""
        t = utc(2026, 8, 1, 10, 0)
        run(FakeCollector([[reading(t)]]))
        # New process an hour later: fresh collector instance, no in-memory bridge.
        run(FakeCollector([[reading(t + timedelta(hours=1))]]))

        spans = list(CoverageSpan.objects.order_by("start"))
        assert len(spans) == 2
        assert spans[0].end < spans[1].start  # the hour between stays unknown


class TestRunRecords:
    def test_successful_run_recorded(self, transactional_db):
        run(FakeCollector([[reading(utc(2026, 8, 1, 10, 0))]]))
        run_row = CollectorRun.objects.get()
        assert run_row.ok is True
        assert run_row.finished is not None
