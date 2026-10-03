"""Backfill storage: vendor history written around what collectors saw live.

The PRD treats backfill as first-class: downtime and the years before
this app existed are healed from each vendor's cloud history. Backfilled
readings land on the same series as live ones, marked BACKFILLED in
coverage so the health page can tell the two apart.

Live knowledge wins. A backfilled interval that overlaps LIVE coverage
is dropped — vendor history is often coarser than the live feed (Enphase
serves quarter-hours where the Envoy is polled every minute), and
storing both would double-count the overlap when energy is integrated.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction

from core.coverage import _utc, record_coverage
from core.models import CoverageSpan, Sample, Series, Source

from .base import Reading


@dataclass
class BackfillResult:
    written: int = 0
    skipped_live: int = 0


def store_backfill(source: Source, readings: list[Reading]) -> BackfillResult:
    """Upsert readings and record BACKFILLED coverage for what was kept.

    Idempotent: re-running a window rewrites the same samples and merges
    into the same spans. Readings the vendor did not return get no
    coverage, so their intervals stay unknown rather than reading zero.
    """
    result = BackfillResult()
    by_metric: dict[str, list[Reading]] = {}
    for r in readings:
        by_metric.setdefault(r.metric, []).append(r)

    with transaction.atomic():
        for metric, batch in by_metric.items():
            batch.sort(key=lambda r: r.ts)
            series, _ = Series.objects.get_or_create(
                source=source, metric=metric, defaults={"unit": batch[0].unit}
            )
            window_end = max(_end(r) for r in batch)
            live = list(
                CoverageSpan.objects.filter(
                    series=series,
                    state=CoverageSpan.State.LIVE,
                    start__lt=window_end,
                    end__gt=_utc(batch[0].ts),
                ).order_by("start")
            )
            kept = []
            for r in batch:
                start, end = _utc(r.ts), _end(r)
                if any(span.start < end and span.end > start for span in live):
                    result.skipped_live += 1
                else:
                    kept.append(r)

            Sample.objects.bulk_create(
                [
                    Sample(series=series, ts=r.ts, duration_s=r.duration_s, value=r.value)
                    for r in kept
                ],
                update_conflicts=True,
                unique_fields=["series", "ts"],
                update_fields=["duration_s", "value"],
            )
            result.written += len(kept)

            # One coverage write per contiguous run, not per reading: a
            # decade of quarter-hours is a few thousand runs, not 300k.
            run_start = run_end = None
            for r in kept:
                start, end = _utc(r.ts), _end(r)
                if run_start is not None and start > run_end:
                    record_coverage(series, run_start, run_end, CoverageSpan.State.BACKFILLED)
                    run_start = None
                if run_start is None:
                    run_start, run_end = start, end
                else:
                    run_end = max(run_end, end)
            if run_start is not None:
                record_coverage(series, run_start, run_end, CoverageSpan.State.BACKFILLED)
    return result


def _end(r: Reading):
    return _utc(r.ts) + timedelta(seconds=r.duration_s)
