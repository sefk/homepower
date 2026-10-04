"""Write one vendor-history interval, replacing whatever it supersedes.

Shared by the Green Button importer and the PG&E gas collector — both
receive PG&E's own meter history, which can arrive more than once and
at a different granularity each time (a daily row later corrected by
hourly ones), so a plain upsert on (series, ts) isn't enough.
"""

from datetime import datetime, timedelta, timezone as dt_timezone

from django.db.models import Max

from core.coverage import clear_coverage, record_coverage
from core.models import CoverageSpan, Sample, Series


def replace_interval(series: Series, ts: datetime, duration_s: int, value: float) -> bool:
    """Store [ts, ts + duration_s) = value as BACKFILLED knowledge.

    ts is aware (fold-correct local time is fine). Returns True if the
    sample was new. Call inside a transaction.
    """
    # Adding a timedelta to an aware zoneinfo datetime silently drops fold
    # and can reinterpret the result at the wrong UTC offset near a DST
    # edge (REVIEW-INSIGHTS: convert to UTC before duration math) — so
    # convert first, then add.
    ts_utc = ts.astimezone(dt_timezone.utc)
    new_end = ts_utc + timedelta(seconds=duration_s)

    # A correction can land at a DIFFERENT ts than the row
    # it supersedes -- e.g. an hourly reading correcting
    # one hour that used to be folded into an earlier
    # daily-granularity row. update_or_create is keyed on
    # exact (series, ts), so it can't find that older
    # sample; left alone, it would still be there at its
    # own ts and _clipped_energy_wh would double-count it
    # (its own full proportional slice, plus the new
    # row's) while coverage looked intact. Delete every
    # OTHER sample on this series whose interval overlaps
    # this row's, and retract the coverage IT justified
    # over its own full interval, not just the overlapping
    # sliver -- a daily reading is one atomic value, so
    # correcting a piece of it invalidates the whole
    # thing; the untouched hours must go back to unknown,
    # not keep reading as known with nothing behind them.
    #
    # Only for series whose every sample comes through here
    # (Green Button imports, the PG&E gas collector): no
    # live collector writes them, so nothing here
    # risks a collector's own LIVE data. Lookback for
    # candidates uses this series' own max sample
    # duration, same reasoning as _clipped_energy_wh's
    # dynamic lookback (core/aggregate.py) -- one cheap
    # aggregate per row/metric at our volumes.
    max_duration = (
        Sample.objects.filter(series=series)
        .aggregate(Max("duration_s"))["duration_s__max"]
        or 0
    )
    overlapping = Sample.objects.filter(
        series=series,
        ts__lt=new_end,
        ts__gte=ts_utc - timedelta(seconds=max_duration),
    ).exclude(ts=ts)
    for other in overlapping:
        other_end = other.ts + timedelta(seconds=other.duration_s)
        if other_end > ts_utc:
            clear_coverage(
                series, other.ts, other_end, CoverageSpan.State.BACKFILLED
            )
            other.delete()

    # Same-ts case: a corrected re-import (e.g. an
    # hour-long row replaced by the real 15-minute one)
    # fixes the Sample via update_or_create below, but
    # stale BACKFILLED coverage the OLD duration justified
    # would otherwise survive past the new, shorter one.
    # clear_coverage revises BACKFILLED knowledge for
    # exactly [ts, end) this sample claims — the new end,
    # or the old one if it reached further — trimming or
    # splitting spans that extend beyond that rather than
    # deleting them outright. That keeps the retraction
    # scoped to this one reading: a one-row correction
    # inside a month-long backfilled span (itself built
    # from many touching rows) doesn't erase the rest of
    # that month, since every other row's own [ts, end) is
    # untouched. Never touches LIVE, which is the
    # collector's own knowledge, not this command's.
    existing = (
        Sample.objects.filter(series=series, ts=ts)
        .values_list("duration_s", flat=True)
        .first()
    )
    clear_end = new_end
    if existing is not None:
        clear_end = max(clear_end, ts_utc + timedelta(seconds=existing))
    clear_coverage(series, ts_utc, clear_end, CoverageSpan.State.BACKFILLED)

    _, created = Sample.objects.update_or_create(
        series=series,
        ts=ts,
        defaults={"duration_s": duration_s, "value": value},
    )
    record_coverage(series, ts_utc, new_end, CoverageSpan.State.BACKFILLED)
    return created
