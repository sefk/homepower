"""Collector base: per-source code is only the vendor conversation.

Everything a collector shares — writing samples, extending live coverage,
recording runs — lives here. A concrete collector implements `setup()`
(vendor auth) and `poll()` (fetch current readings) and nothing else.

Coverage semantics: each successful poll covers [ts, ts + duration). If
the previous successful poll was recent (within GRACE_POLLS intervals),
the span is written from that poll's ts so scheduler jitter can never
shred contiguous collection into micro-gaps. The bridge is in-memory
only — after a process restart the first poll stands alone, so downtime
reads as unknown until a backfill heals it. That is the PRD's
"gap detected on restart" transition.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta

from asgiref.sync import sync_to_async
from django.utils import timezone

from core.coverage import record_coverage
from core.models import CollectorRun, CoverageSpan, Sample, Series, Source

logger = logging.getLogger(__name__)

# How stale the previous poll may be and still count as contiguous.
GRACE_POLLS = 2


@dataclass
class Reading:
    metric: str
    unit: str
    ts: datetime  # UTC interval start
    duration_s: int
    value: float


class Collector(ABC):
    """One vendor integration. Subclasses set the class attrs and poll()."""

    slug: str
    name: str
    kind: str
    poll_interval_s: int
    native_resolution_s: int
    # Metrics whose consecutive readings bridge coverage regardless of the
    # gap between them. Correct only for cumulative registers, where the
    # difference of two readings is exact knowledge of the whole interval.
    always_bridge: frozenset[str] = frozenset()

    @property
    def grace_s(self) -> int:
        """Max gap between readings that still bridges live coverage.

        Point-in-time pollers tolerate jitter (2 polls). Interval-series
        vendors (SolarEdge quarters) must override this to one resolution
        step: an omitted interval is unknown, and a wide grace would
        quietly cover it.
        """
        return GRACE_POLLS * self.poll_interval_s

    def __init__(self):
        self._source: Source | None = None
        self._last_ts: dict[str, datetime] = {}  # metric -> last successful poll ts

    async def setup(self) -> None:
        """Ensure the Source row exists; subclasses add vendor auth."""
        self._source = await sync_to_async(self._ensure_source)()

    @abstractmethod
    async def poll(self) -> list[Reading]:
        """Fetch current readings from the vendor. Raise on failure."""

    async def run_once(self) -> bool:
        """One poll attempt: fetch, store, record the run. Never raises."""
        started = timezone.now()
        try:
            readings = await self.poll()
        except Exception as exc:
            logger.warning("%s poll failed: %s", self.slug, exc)
            await sync_to_async(self._record_run)(started, ok=False, message=str(exc))
            return False
        try:
            await sync_to_async(self._store)(readings)
        except Exception as exc:
            # A storage bug must surface as a visible failed run, not kill
            # the whole process in a launchd crash loop.
            logger.exception("%s store failed", self.slug)
            await sync_to_async(self._record_run)(
                started, ok=False, message=f"store failed: {exc!r}"
            )
            return False
        await sync_to_async(self._record_run)(
            started, ok=True, message=f"{len(readings)} readings"
        )
        return True

    # --- sync internals (called via sync_to_async) ---

    def _ensure_source(self) -> Source:
        source, _ = Source.objects.update_or_create(
            slug=self.slug,
            defaults={
                "name": self.name,
                "kind": self.kind,
                "poll_interval_s": self.poll_interval_s,
                "native_resolution_s": self.native_resolution_s,
            },
        )
        return source

    def _store(self, readings: list[Reading]) -> None:
        grace = timedelta(seconds=self.grace_s)
        for r in readings:
            series, _ = Series.objects.get_or_create(
                source=self._source, metric=r.metric, defaults={"unit": r.unit}
            )
            Sample.objects.update_or_create(
                series=series,
                ts=r.ts,
                defaults={"duration_s": r.duration_s, "value": r.value},
            )
            prev = self._last_ts.get(r.metric)
            # Bridge only forward: a collector that re-reads a window (e.g.
            # SolarEdge's one-hour lookback) hands us timestamps older than
            # prev, and bridging those would invert the span. Re-read
            # readings still record their own interval, which merges into
            # the existing span.
            bridge = (
                prev is not None
                and prev <= r.ts
                and (r.metric in self.always_bridge or r.ts - prev <= grace)
            )
            start = prev if bridge else r.ts
            record_coverage(
                series, start, r.ts + timedelta(seconds=r.duration_s), CoverageSpan.State.LIVE
            )
            if prev is None or r.ts > prev:
                self._last_ts[r.metric] = r.ts

    def _record_run(self, started: datetime, ok: bool, message: str) -> None:
        CollectorRun.objects.create(
            source=self._source,
            started=started,
            finished=timezone.now(),
            ok=ok,
            message=message,
        )
