"""Import PG&E Green Button usage-export CSVs.

Core owns this, not the collectors package — there's no vendor
conversation here, just a file the user downloaded by hand from pge.com.
It exists because the Eagle 3 wasn't always installed and, even once it
is, its history only goes back as far as the process has been running:
Green Button is the backfill path to PG&E's own meter history (PRD:
"every source has a vendor cloud with history behind it").

Green Button CSVs open with a preamble (Name/Address/Account lines)
before the real header row, and PG&E has shipped more than one column
layout: a single USAGE column (negative = export on NEM accounts,
all-positive on import-only meters) and a two-column IMPORT (kWh)/
EXPORT (kWh) split for NEM accounts that break it out already. Interval
length is either explicit (END TIME present) or has to be inferred from
the gap to the next row.

Rows land on the `eagle` Source as grid_import_wh / grid_export_wh Wh
samples, alongside whatever the Eagle 3 itself has pushed live.

The same download carries natural-gas files: daily rows with a
"USAGE (therms)" column. Those go to their own `pge_gas` Source as
gas_wh, converted to Wh so every energy series shares one unit (the
dashboards turn it back into therms). Nothing else writes gas, so
there is no collector behind that Source.
update_or_create on (series, ts) makes re-import idempotent; coverage is
recorded BACKFILLED per row, and core.coverage.record_coverage merges
touching same-state spans into one, so a whole file becomes one span
per contiguous run without this command doing that bookkeeping itself.
"""

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from collectors.eagle import ensure_source
from core.coverage import clear_coverage, record_coverage
from core.models import CoverageSpan, Sample, Series, Source

# PG&E's DATE + TIME columns, tried in order. Older exports write
# 08/01/2025; current ones write 2025-08-01.
_DATETIME_FORMATS = (
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
)
# Current exports give END TIME as the interval's last minute (00:00 ->
# 00:59, 00:15 -> 00:29), not its exclusive end. An end one minute shy
# of a quarter-hour boundary is that inclusive form.
_INCLUSIVE_END_MINUTE_STEP = 15
# No END TIME column and only one row in the file: nothing to infer
# spacing from. Picked as a plausible hourly default rather than left 0.
_FALLBACK_DURATION_S = 3600
# PG&E bills the US therm: 100,000 BTU.
WH_PER_THERM = 29_307.1
GAS_SOURCE_SLUG = "pge_gas"


@dataclass
class _Row:
    ts: datetime  # aware America/Los_Angeles, fold-correct
    duration_s: int | None  # None => infer from the gap to the next row
    values: list[tuple[str, float]]  # (metric, wh) to write, 1 or 2 entries


def _parse_dt(date_str: str, time_str: str) -> datetime:
    text = f"{date_str.strip()} {time_str.strip()}"
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable date/time {text!r}")


def _find_header(rows: list[list[str]]) -> tuple[int, list[str]] | None:
    """Scan past PG&E's Name/Address/Account preamble for the header row."""
    for i, row in enumerate(rows):
        fields = [f.strip().upper() for f in row]
        if (
            "TYPE" in fields
            and "DATE" in fields
            and "START TIME" in fields
            and any(f.startswith(("USAGE", "IMPORT", "EXPORT")) for f in fields)
        ):
            return i, fields
    return None


def _is_gas(fields: list[str]) -> bool:
    """Gas exports name their unit in the USAGE header: "USAGE (THERMS)"."""
    return any(f.startswith("USAGE") and "THERM" in f for f in fields)


def ensure_gas_source() -> Source:
    source, _ = Source.objects.get_or_create(
        slug=GAS_SOURCE_SLUG,
        defaults={
            "name": "PG&E Gas",
            "kind": Source.Kind.GAS,
            # Only ever imported by hand, a day at a time.
            "poll_interval_s": 86400,
            "native_resolution_s": 86400,
        },
    )
    return source


def _col(fields: list[str], name: str) -> int | None:
    return fields.index(name) if name in fields else None


def _col_prefix(fields: list[str], prefix: str) -> int | None:
    for i, f in enumerate(fields):
        if f.startswith(prefix):
            return i
    return None


class Command(BaseCommand):
    help = "Import PG&E Green Button usage-export CSVs: electric into grid_import_wh/grid_export_wh, gas into gas_wh"

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+", help="Green Button CSV export path(s)")

    def handle(self, *args, **options):
        source = ensure_source()
        import_series, _ = Series.objects.get_or_create(
            source=source, metric="grid_import_wh", defaults={"unit": "Wh"}
        )
        export_series, _ = Series.objects.get_or_create(
            source=source, metric="grid_export_wh", defaults={"unit": "Wh"}
        )
        series_by_metric = {
            "grid_import_wh": import_series,
            "grid_export_wh": export_series,
        }

        grand_created = grand_updated = grand_unparseable = 0
        grand_totals: dict[str, float] = {}
        grand_min = grand_max = None

        for path in options["files"]:
            created, updated, unparseable, totals, ts_min, ts_max = self._import_file(
                path, series_by_metric
            )
            if created + updated == 0:
                raise CommandError(f"{path}: no parseable Green Button rows found")
            self.stdout.write(
                f"{path}: {created} created, {updated} updated, "
                f"{unparseable} unparseable row(s)"
            )
            grand_created += created
            grand_updated += updated
            grand_unparseable += unparseable
            for metric, wh in totals.items():
                grand_totals[metric] = grand_totals.get(metric, 0.0) + wh
            if ts_min is not None:
                grand_min = ts_min if grand_min is None else min(grand_min, ts_min)
                grand_max = ts_max if grand_max is None else max(grand_max, ts_max)

        self.stdout.write(
            f"total: {grand_created} created, {grand_updated} updated, "
            f"{grand_unparseable} unparseable row(s)"
        )
        if grand_min is not None:
            self.stdout.write(f"range: {grand_min.isoformat()} .. {grand_max.isoformat()}")
        for metric, wh in grand_totals.items():
            self.stdout.write(f"{metric}: {wh / 1000:.2f} kWh")

    def _import_file(self, path, series_by_metric):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            raw_rows = list(csv.reader(fh))

        totals: dict[str, float] = {}
        header = _find_header(raw_rows)
        if header is None:
            return 0, 0, len(raw_rows), totals, None, None
        header_i, fields = header
        gas = _is_gas(fields)
        if gas and "gas_wh" not in series_by_metric:
            series_by_metric["gas_wh"], _ = Series.objects.get_or_create(
                source=ensure_gas_source(), metric="gas_wh", defaults={"unit": "Wh"}
            )

        date_idx = _col(fields, "DATE")
        start_idx = _col(fields, "START TIME")
        end_idx = _col(fields, "END TIME")
        usage_idx = _col_prefix(fields, "USAGE")
        import_idx = _col_prefix(fields, "IMPORT")
        export_idx = _col_prefix(fields, "EXPORT")

        tz = timezone.get_current_timezone()
        parsed: list[_Row] = []
        unparseable = 0
        prev_naive = None
        fold = 0

        # Every row whose DATE/START TIME parsed, valid or not — used to
        # bound duration inference. A row can be unparseable because its
        # USAGE/IMPORT/EXPORT value is garbage while its timestamp is
        # fine; that timestamp is still real information about where the
        # next reading starts, and dropping it from this sequence (instead
        # of just from `parsed`, below) would let its neighbor's inferred
        # duration silently stretch across the interval the bad row
        # actually covered.
        sequence: list[datetime] = []
        row_seq_index: list[int] = []  # parsed[i] -> its index into sequence

        for raw in raw_rows[header_i + 1 :]:
            if not raw or all(not c.strip() for c in raw):
                continue
            try:
                start_naive = _parse_dt(raw[date_idx], raw[start_idx])
            except (ValueError, IndexError):
                unparseable += 1
                continue
            # DST fall-back: PG&E's export is chronological, so a wall
            # time that doesn't advance past its predecessor marks the
            # second (standard-time) pass through the ambiguous hour.
            # Same approach as collectors/solaredge.py parse_power_details.
            # fold=1 only while (a) this wall time is genuinely ambiguous
            # (the two folds resolve to different offsets) and (b) a
            # non-advancing timestamp told us we're in the second pass.
            # (a) alone isn't enough — the first pass through the repeated
            # hour is ambiguous too; (b) alone would latch fold past the
            # ambiguous hour, so a year-plus export spanning TWO fall-backs
            # would misread the next one's first pass and collide rows on
            # (series, ts).
            ambiguous = (
                start_naive.replace(tzinfo=tz, fold=0).utcoffset()
                != start_naive.replace(tzinfo=tz, fold=1).utcoffset()
            )
            if prev_naive is not None and start_naive <= prev_naive:
                fold = 1
            elif (
                fold
                and prev_naive is not None
                and start_naive - prev_naive > timedelta(hours=1)
            ):
                # Jumped clean past the ambiguous hour (rows inside a
                # second pass advance by at most the interval, <= 1h) —
                # covers a file whose very next row lands in a LATER
                # fall-back's repeated hour, where the ambiguity check
                # below wouldn't release the latch.
                fold = 0
            if not ambiguous:
                fold = 0
            prev_naive = start_naive
            ts = start_naive.replace(tzinfo=tz, fold=fold)
            sequence.append(ts)

            try:
                values: list[tuple[str, float]] = []
                if gas:
                    therms = float(raw[usage_idx])
                    if therms < 0:
                        raise ValueError("negative gas usage")
                    values.append(("gas_wh", therms * WH_PER_THERM))
                elif usage_idx is not None:
                    # Single-column USAGE only tells us one direction per
                    # row, but grid_hourly_wh takes the coverage of BOTH
                    # grid_import_wh and grid_export_wh for a window (it
                    # doesn't know this file is single-column). Writing an
                    # explicit 0.0 for the direction the sign didn't pick
                    # makes that a known zero, same as the two-column
                    # variant's explicit "0.000" — otherwise an
                    # import-only meter would leave grid_export_wh with no
                    # coverage at all and grid_hourly_wh would report 0%
                    # coverage for windows this file fully describes.
                    kwh = float(raw[usage_idx])
                    if kwh >= 0:
                        values.append(("grid_import_wh", kwh * 1000))
                        values.append(("grid_export_wh", 0.0))
                    else:
                        values.append(("grid_export_wh", -kwh * 1000))
                        values.append(("grid_import_wh", 0.0))
                else:
                    if import_idx is not None and raw[import_idx].strip() != "":
                        values.append(("grid_import_wh", float(raw[import_idx]) * 1000))
                    if export_idx is not None and raw[export_idx].strip() != "":
                        values.append(("grid_export_wh", float(raw[export_idx]) * 1000))
                if not values:
                    raise ValueError("no usage value in row")

                duration_s = None
                if end_idx is not None and raw[end_idx].strip():
                    end_naive = _parse_dt(raw[date_idx], raw[end_idx])
                    end_ts = end_naive.replace(tzinfo=tz, fold=fold)
                    if end_ts <= ts:
                        # Wall clock didn't advance. Two different things
                        # produce this: a genuine midnight wrap (23:45 ->
                        # 00:00), or an interval straddling the DST
                        # fall-back itself (START 01:45 in the first/PDT
                        # pass, END "01:00" meaning 01:00 in the second/PST
                        # pass — same digits, later in real time). Try the
                        # fall-back reading first: reinterpreting END TIME
                        # at fold=1 is a no-op for any unambiguous instant,
                        # so it only changes the result when that's really
                        # the fix. Only fall through to the midnight wrap
                        # when that still doesn't produce a later instant.
                        #
                        # Comparing via UTC, not `candidate > ts` directly:
                        # two datetimes sharing the same zoneinfo tzinfo
                        # object take Python's wall-clock comparison
                        # shortcut regardless of fold (REVIEW-INSIGHTS —
                        # same pitfall as duration math, just on `>`
                        # instead of `-`), which would compare 01:00 PST
                        # against 01:45 PDT as if both were the same
                        # offset and get this exact case backwards.
                        candidate = end_naive.replace(tzinfo=tz, fold=1)
                        if candidate.astimezone(dt_timezone.utc) > ts.astimezone(dt_timezone.utc):
                            end_ts = candidate
                        else:
                            end_naive += timedelta(days=1)
                            end_ts = end_naive.replace(tzinfo=tz, fold=fold)
                    # REVIEW-INSIGHTS: convert to UTC before duration math —
                    # same-tzinfo local datetimes subtract by wall clock and
                    # lie across a DST edge.
                    duration_s = int(
                        (
                            end_ts.astimezone(dt_timezone.utc)
                            - ts.astimezone(dt_timezone.utc)
                        ).total_seconds()
                    )
                    if (end_naive.minute + 1) % _INCLUSIVE_END_MINUTE_STEP == 0:
                        duration_s += 60
            except (ValueError, IndexError):
                unparseable += 1
                continue

            parsed.append(_Row(ts=ts, duration_s=duration_s, values=values))
            row_seq_index.append(len(sequence) - 1)

        # Infer duration from the gap to the next KNOWN timestamp wherever
        # END TIME was absent — the next entry in `sequence`, not the next
        # entry in `parsed`. Those differ exactly when a row in between had
        # a good timestamp but a bad value: using `sequence` stops the
        # duration at that row's start instead of stretching past it into
        # an interval this file never actually described. UTC for the same
        # reason as above (wall-clock subtraction lies across a DST edge).
        for i, row in enumerate(parsed):
            if row.duration_s is not None:
                continue
            seq_i = row_seq_index[i]
            if seq_i + 1 < len(sequence):
                gap = (
                    sequence[seq_i + 1].astimezone(dt_timezone.utc)
                    - row.ts.astimezone(dt_timezone.utc)
                ).total_seconds()
                row.duration_s = int(gap)
            elif i > 0:
                row.duration_s = parsed[i - 1].duration_s
            else:
                row.duration_s = _FALLBACK_DURATION_S

        created = updated = 0
        ts_min = ts_max = None
        with transaction.atomic():
            for row in parsed:
                # Adding a timedelta to an aware zoneinfo datetime silently
                # drops fold and can reinterpret the result at the wrong
                # UTC offset near a DST edge (REVIEW-INSIGHTS: convert to
                # UTC before duration math) — so convert first, then add.
                ts_utc = row.ts.astimezone(dt_timezone.utc)
                new_end = ts_utc + timedelta(seconds=row.duration_s)
                for metric, wh in row.values:
                    series = series_by_metric[metric]

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
                    # Scoped to grid_import_wh/grid_export_wh/gas_wh only:
                    # this importer is their exclusive writer (no collector
                    # ever touches these metrics), so nothing here
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
                    ).exclude(ts=row.ts)
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
                        Sample.objects.filter(series=series, ts=row.ts)
                        .values_list("duration_s", flat=True)
                        .first()
                    )
                    clear_end = new_end
                    if existing is not None:
                        clear_end = max(clear_end, ts_utc + timedelta(seconds=existing))
                    clear_coverage(series, ts_utc, clear_end, CoverageSpan.State.BACKFILLED)

                    _, was_created = Sample.objects.update_or_create(
                        series=series,
                        ts=row.ts,
                        defaults={"duration_s": row.duration_s, "value": wh},
                    )
                    created += was_created
                    updated += not was_created
                    totals[metric] = totals.get(metric, 0.0) + wh
                    record_coverage(series, ts_utc, new_end, CoverageSpan.State.BACKFILLED)
                ts_min = row.ts if ts_min is None else min(ts_min, row.ts)
                ts_max = row.ts if ts_max is None else max(ts_max, row.ts)

        return created, updated, unparseable, totals, ts_min, ts_max
