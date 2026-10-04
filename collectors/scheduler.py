"""Per-source asyncio loops. One failing source never stalls another.

Each collector runs on its own cadence (fixed, unless the collector
varies next_interval_s()) measured poll-start to poll-start, so a slow
vendor response doesn't drift the schedule. Setup failures (bad
credentials, vendor down) retry with backoff instead of killing the
loop — the source just stays absent, which the coverage model already
treats honestly as unknown.
"""

import asyncio
import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.utils import timezone

from core.models import CollectorRun

from .base import Collector

logger = logging.getLogger(__name__)

SETUP_RETRY_S = 300
RUN_RETENTION_DAYS = 30


async def run_collector(collector: Collector) -> None:
    """Set up, then poll forever on the collector's cadence."""
    while True:
        try:
            await collector.setup()
            break
        except Exception as exc:
            logger.warning(
                "%s setup failed, retrying in %ss: %s",
                collector.slug, SETUP_RETRY_S, exc,
            )
            await asyncio.sleep(SETUP_RETRY_S)

    logger.info("%s collector started (every %ss)", collector.slug, collector.poll_interval_s)
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await collector.run_once()
        elapsed = loop.time() - started
        await asyncio.sleep(max(0.0, collector.next_interval_s() - elapsed))


async def prune_runs() -> None:
    """Daily cleanup so per-minute CollectorRun rows stay bounded."""
    while True:
        cutoff = timezone.now() - timedelta(days=RUN_RETENTION_DAYS)
        deleted, _ = await sync_to_async(
            CollectorRun.objects.filter(started__lt=cutoff).delete
        )()
        if deleted:
            logger.info("pruned %d collector runs older than %dd", deleted, RUN_RETENTION_DAYS)
        await asyncio.sleep(24 * 3600)


async def run_all(collectors: list[Collector]) -> None:
    """Run every enabled collector plus housekeeping, concurrently."""
    tasks = [run_collector(c) for c in collectors]
    tasks.append(prune_runs())
    await asyncio.gather(*tasks)
