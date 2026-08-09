"""Green Button CSV import: header scanning, column variants, duration
inference, DST fold, idempotency, coverage, and grid_hourly_wh."""

from datetime import date, datetime, timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.aggregate import grid_hourly_wh
from core.coverage import record_coverage, uncovered
from core.models import CoverageSpan, Sample, Series, Source

from .conftest import utc

# Single USAGE column, negative = export, explicit hourly END TIME.
# Preamble mimics PG&E's Name/Address/Account block; the address line is
# quoted with an embedded comma to prove quoted fields survive.
SINGLE_USAGE_CSV = """Name:,Sef Kloninger
Address:,"635 Central Ave, CA"
Account Number:,1234567890

TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST
Electric usage,08/01/2025,00:00,01:00,1.200,kWh,$0.75
Electric usage,08/01/2025,01:00,02:00,-0.500,kWh,$0.00
"""

# IMPORT/EXPORT two-column NEM variant, 15-minute rows, no END TIME —
# duration must be inferred from the gap to the next row.
IMPORT_EXPORT_CSV = """Name:,Sef Kloninger
Account Number:,1234567890

TYPE,DATE,START TIME,IMPORT (kWh),EXPORT (kWh),UNITS,COST
Electric usage,08/02/2025,00:00,0.300,0.000,kWh,$0.19
Electric usage,08/02/2025,00:15,0.100,0.050,kWh,$0.06
Electric usage,08/02/2025,00:30,0.000,0.200,kWh,$0.00
"""

# 2025-11-02 is the US fall-back Sunday: wall time 01:00-01:15 occurs
# twice. File order is chronological (PDT pass, then PST pass).
DST_FALLBACK_CSV = """TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST
Electric usage,11/02/2025,01:00,01:15,0.100,kWh,$0.06
Electric usage,11/02/2025,01:00,01:15,0.100,kWh,$0.06
"""

# A file spanning TWO fall-back transitions (2025-11-02 and 2026-11-01).
# The fold latch must release after the first repeated hour, or the
# second transition's first (daylight) pass reads as standard time and
# collides with the real second pass on (series, ts).
DST_TWO_FALLBACKS_CSV = """TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST
Electric usage,11/02/2025,01:00,01:15,0.100,kWh,$0.06
Electric usage,11/02/2025,01:00,01:15,0.200,kWh,$0.12
Electric usage,11/01/2026,01:00,01:15,0.300,kWh,$0.18
Electric usage,11/01/2026,01:00,01:15,0.400,kWh,$0.24
"""

GARBAGE_CSV = """this is not a Green Button export
just some random text
with no header row at all
"""

# Single-column, always-positive (no NEM) — the import-only meter case.
IMPORT_ONLY_CSV = """TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST
Electric usage,08/01/2025,00:00,01:00,1.000,kWh,$0.62
"""

# No END TIME, and the middle row's USAGE is unparseable even though its
# own DATE/START TIME is fine — duration inference must stop at the
# middle row's timestamp, not stretch through it to the third row.
GAP_ROW_CSV = """TYPE,DATE,START TIME,USAGE,UNITS,COST
Electric usage,08/03/2025,00:00,0.300,kWh,$0.19
Electric usage,08/03/2025,00:15,not-a-number,kWh,$0.00
Electric usage,08/03/2025,00:45,0.100,kWh,$0.06
"""

# The interval itself straddles the fall-back: START 01:45 in the first
# (PDT) pass, END "01:00" meaning 01:00 in the second (PST) pass — same
# wall-clock digits as the start's, but 15 real minutes later, not a
# midnight wrap almost a day away.
DST_STRADDLE_CSV = """TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST
Electric usage,11/02/2025,01:45,01:00,0.100,kWh,$0.06
"""

# First import: no END TIME, one row -> falls back to the 1-hour default.
UNCORRECTED_HOUR_CSV = """TYPE,DATE,START TIME,USAGE,UNITS,COST
Electric usage,08/05/2025,00:00,1.000,kWh,$0.62
"""

# Re-import of the same reading, corrected to its real 15-minute interval.
CORRECTED_QUARTER_CSV = """TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST
Electric usage,08/05/2025,00:00,00:15,1.000,kWh,$0.62
"""

# A daily-granularity row, later partly corrected by an hourly one at a
# DIFFERENT ts (inside the day, not at its 00:00 start) -- the case
# where update_or_create's exact-(series, ts) key can't find the row it
# should really be replacing.
DAILY_ROW_CSV = """TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST
Electric usage,08/06/2025,00:00,00:00,24.000,kWh,$0.00
"""

CORRECTED_HOUR_INSIDE_DAY_CSV = """TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST
Electric usage,08/06/2025,05:00,06:00,2.000,kWh,$0.00
"""


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return str(path)


def _full_day_single_usage_csv(date_str, hourly_kwh=0.500):
    """24 hourly single-USAGE rows covering a full local day -- enough
    coverage for both the peak (4-9pm) and off-peak sub-windows."""
    lines = ["TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST"]
    for h in range(24):
        start = f"{h:02d}:00"
        end = f"{(h + 1) % 24:02d}:00"
        lines.append(f"Electric usage,{date_str},{start},{end},{hourly_kwh:.3f},kWh,$0.00")
    return "\n".join(lines) + "\n"


def _daily_rows_csv(start_date_str, num_days, daily_kwh=24.0):
    """One row per day, each spanning the full 24h (00:00 -> 00:00 next
    day) -- consecutive rows' BACKFILLED spans touch and merge into one
    continuous multi-day span on import, standing in for a "month-long"
    backfilled history without generating hundreds of hourly rows."""
    lines = ["TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST"]
    d = datetime.strptime(start_date_str, "%m/%d/%Y").date()
    for _ in range(num_days):
        lines.append(f"Electric usage,{d.strftime('%m/%d/%Y')},00:00,00:00,{daily_kwh:.3f},kWh,$0.00")
        d += timedelta(days=1)
    return "\n".join(lines) + "\n"


def _single_hour_csv(date_str, hour, kwh):
    end_hour = (hour + 1) % 24
    return (
        "TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST\n"
        f"Electric usage,{date_str},{hour:02d}:00,{end_hour:02d}:00,{kwh:.3f},kWh,$0.00\n"
    )


@pytest.mark.django_db
class TestSingleUsageColumn:
    def test_positive_is_import_negative_is_export(self, tmp_path):
        path = _write(tmp_path, "usage.csv", SINGLE_USAGE_CSV)
        call_command("import_greenbutton", path)

        source = Source.objects.get(slug="eagle")
        assert source.kind == Source.Kind.GRID

        # Row 1 (usage +1.200) is the real import reading; row 2 is the
        # explicit 0.0 written for the direction its sign didn't pick.
        imp = Sample.objects.get(series__metric="grid_import_wh", ts=utc(2025, 8, 1, 7, 0))
        assert imp.value == pytest.approx(1200.0)
        assert imp.duration_s == 3600
        imp_zero = Sample.objects.get(series__metric="grid_import_wh", ts=utc(2025, 8, 1, 8, 0))
        assert imp_zero.value == 0.0

        exp = Sample.objects.get(series__metric="grid_export_wh", ts=utc(2025, 8, 1, 8, 0))
        assert exp.value == pytest.approx(500.0)  # abs(-0.500 kWh) * 1000
        exp_zero = Sample.objects.get(series__metric="grid_export_wh", ts=utc(2025, 8, 1, 7, 0))
        assert exp_zero.value == 0.0

    def test_reports_unparseable_not_fatal(self, tmp_path, capsys):
        content = SINGLE_USAGE_CSV + "Electric usage,08/01/2025,not-a-time,,garbage,kWh,$0\n"
        path = _write(tmp_path, "usage.csv", content)
        call_command("import_greenbutton", path)
        # 2 rows x (real reading + explicit opposite-direction zero) each
        assert Sample.objects.count() == 4  # the bad row didn't sink the file

    def test_idempotent_reimport(self, tmp_path):
        path = _write(tmp_path, "usage.csv", SINGLE_USAGE_CSV)
        call_command("import_greenbutton", path)
        call_command("import_greenbutton", path)
        assert Sample.objects.count() == 4

    def test_import_only_meter_gives_grid_hourly_wh_full_coverage(self, tmp_path):
        """A single-USAGE, always-positive file (no NEM) still has to make
        grid_export_wh a known zero — otherwise grid_hourly_wh's MIN of
        both series' coverage sees an unwritten export series and reports
        0% for a window this file fully describes."""
        path = _write(tmp_path, "import_only.csv", IMPORT_ONLY_CSV)
        call_command("import_greenbutton", path)

        window = (utc(2025, 8, 1, 7, 0), utc(2025, 8, 1, 8, 0))
        result = grid_hourly_wh(*window)
        assert result.imported_wh == pytest.approx(1000.0)
        assert result.exported_wh == 0.0
        assert result.coverage == pytest.approx(1.0)


@pytest.mark.django_db
class TestImportExportColumns:
    def test_both_columns_written_including_zero(self, tmp_path):
        path = _write(tmp_path, "nem.csv", IMPORT_EXPORT_CSV)
        call_command("import_greenbutton", path)

        imports = list(Sample.objects.filter(series__metric="grid_import_wh").order_by("ts"))
        exports = list(Sample.objects.filter(series__metric="grid_export_wh").order_by("ts"))
        assert [s.value for s in imports] == pytest.approx([300.0, 100.0, 0.0])
        assert [s.value for s in exports] == pytest.approx([0.0, 50.0, 200.0])
        # a present "0.000" is a known zero reading, not an absent one
        assert imports[2].value == 0.0

    def test_duration_inferred_from_next_row_gap(self, tmp_path):
        path = _write(tmp_path, "nem.csv", IMPORT_EXPORT_CSV)
        call_command("import_greenbutton", path)
        durations = {
            s.duration_s
            for s in Sample.objects.filter(series__metric="grid_import_wh")
        }
        assert durations == {900}  # 15 minutes, including the last row

    def test_coverage_merges_contiguous_rows_into_one_span(self, tmp_path):
        path = _write(tmp_path, "nem.csv", IMPORT_EXPORT_CSV)
        call_command("import_greenbutton", path)

        for metric in ("grid_import_wh", "grid_export_wh"):
            spans = CoverageSpan.objects.filter(series__metric=metric)
            assert spans.count() == 1
            span = spans.get()
            assert span.state == CoverageSpan.State.BACKFILLED
            assert span.start == utc(2025, 8, 2, 7, 0)  # 00:00 PDT
            assert span.end == utc(2025, 8, 2, 7, 45)  # 00:45 PDT


@pytest.mark.django_db
class TestDstFallback:
    def test_repeated_wall_time_becomes_two_distinct_samples(self, tmp_path):
        path = _write(tmp_path, "dst.csv", DST_FALLBACK_CSV)
        call_command("import_greenbutton", path)

        samples = list(Sample.objects.filter(series__metric="grid_import_wh").order_by("ts"))
        assert len(samples) == 2
        assert samples[1].ts - samples[0].ts == timedelta(hours=1)
        # 01:00 PDT (fold=0) = 08:00 UTC, 01:00 PST (fold=1) = 09:00 UTC
        assert samples[0].ts == utc(2025, 11, 2, 8, 0)
        assert samples[1].ts == utc(2025, 11, 2, 9, 0)

    def test_second_fallback_a_year_later_keeps_all_four_samples(self, tmp_path):
        """The fold latch resets once the ambiguous hour ends; a year-plus
        export spanning two fall-backs keeps all four repeated-hour rows
        as distinct instants instead of colliding the second pair."""
        path = _write(tmp_path, "dst_two.csv", DST_TWO_FALLBACKS_CSV)
        call_command("import_greenbutton", path)

        samples = list(Sample.objects.filter(series__metric="grid_import_wh").order_by("ts"))
        assert [s.value for s in samples] == [100.0, 200.0, 300.0, 400.0]
        # each transition's pair is PDT then PST, one real hour apart
        assert samples[1].ts - samples[0].ts == timedelta(hours=1)
        assert samples[3].ts - samples[2].ts == timedelta(hours=1)

    def test_interval_straddling_the_transition_is_15_minutes_not_a_day(self, tmp_path):
        """START 01:45 (PDT) -> END "01:00" (PST) is a real 15-minute
        interval, not a midnight wrap: the wall clock falls back exactly
        inside it. Naively treating end <= start as a midnight wrap here
        would push the end a full day forward instead of 15 minutes."""
        path = _write(tmp_path, "dst_straddle.csv", DST_STRADDLE_CSV)
        call_command("import_greenbutton", path)

        sample = Sample.objects.get(series__metric="grid_import_wh")
        assert sample.duration_s == 900


@pytest.mark.django_db
class TestGapRowDoesNotStretchNeighborDuration:
    def test_middle_interval_stays_uncovered(self, tmp_path):
        path = _write(tmp_path, "gap.csv", GAP_ROW_CSV)
        call_command("import_greenbutton", path)

        imports = list(
            Sample.objects.filter(series__metric="grid_import_wh").order_by("ts")
        )
        assert len(imports) == 2  # the middle (unparseable) row wrote nothing
        assert imports[0].ts == utc(2025, 8, 3, 7, 0)
        assert imports[1].ts == utc(2025, 8, 3, 7, 45)
        # Duration must stop at the rejected row's timestamp (00:15, 15 min
        # later) — not stretch all the way to the next valid row (00:45,
        # 45 min later), which would silently claim energy for an interval
        # this file never actually described.
        assert imports[0].duration_s == 900
        assert imports[1].duration_s == 900  # falls back to the prior row's duration

        series = imports[0].series
        gaps = uncovered(series, utc(2025, 8, 3, 7, 0), utc(2025, 8, 3, 8, 0))
        assert gaps == [(utc(2025, 8, 3, 7, 15), utc(2025, 8, 3, 7, 45))]

        # No energy was fabricated for the uncovered middle interval.
        result = grid_hourly_wh(utc(2025, 8, 3, 7, 15), utc(2025, 8, 3, 7, 45))
        assert result.imported_wh == 0.0


@pytest.mark.django_db
class TestGarbageFile:
    def test_no_parseable_rows_raises_command_error(self, tmp_path):
        path = _write(tmp_path, "garbage.csv", GARBAGE_CSV)
        with pytest.raises(CommandError):
            call_command("import_greenbutton", path)
        assert Sample.objects.count() == 0


@pytest.mark.django_db
class TestCorrectedReimport:
    def test_stale_backfilled_coverage_is_replaced_not_accreted(self, tmp_path):
        """First import: no END TIME, defaults to a 1-hour interval.
        Re-import: the same reading, corrected to its real 15 minutes.
        update_or_create fixes the Sample either way, but the ORIGINAL
        1-hour BACKFILLED span must not survive re-import — otherwise the
        45 minutes the corrected file no longer describes would stay
        falsely marked as known."""
        t0 = utc(2025, 8, 5, 7, 0)  # 00:00 PDT

        first = _write(tmp_path, "uncorrected.csv", UNCORRECTED_HOUR_CSV)
        call_command("import_greenbutton", first)
        sample = Sample.objects.get(series__metric="grid_import_wh")
        assert sample.duration_s == 3600

        # A LIVE span (the Eagle's own knowledge, not this command's to
        # touch) overlapping the same range must survive re-import.
        live_series = sample.series
        record_coverage(
            live_series, t0 + timedelta(minutes=20), t0 + timedelta(minutes=25),
            CoverageSpan.State.LIVE,
        )

        second = _write(tmp_path, "corrected.csv", CORRECTED_QUARTER_CSV)
        call_command("import_greenbutton", second)

        sample.refresh_from_db()
        assert sample.duration_s == 900

        backfilled = CoverageSpan.objects.filter(
            series__metric="grid_import_wh", state=CoverageSpan.State.BACKFILLED
        )
        assert backfilled.count() == 1
        span = backfilled.get()
        assert span.start == t0
        assert span.end == t0 + timedelta(minutes=15)

        live = CoverageSpan.objects.filter(
            series__metric="grid_import_wh", state=CoverageSpan.State.LIVE
        )
        assert live.count() == 1  # untouched
        assert live.get().start == t0 + timedelta(minutes=20)


@pytest.mark.django_db
class TestWiredIntoCostViews:
    """Issue #5's whole point: Green Button backfill has to actually reach
    the UI, not just sit in the database. A period with ONLY Green Button
    data (no Eagle 3 / demand_w at all) must still populate the peak table
    and cost heatmap through grid_hourly_wh."""

    def test_peak_table_shows_real_dollars_from_green_button_only_data(
        self, tmp_path, client, monkeypatch
    ):
        from billing.rates import SUMMER_OFFPEAK, SUMMER_PEAK

        frozen_today = date(2026, 8, 10)
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: frozen_today)

        path = _write(
            tmp_path, "full_day.csv", _full_day_single_usage_csv("08/10/2026", hourly_kwh=0.5)
        )
        call_command("import_greenbutton", path)

        resp = client.get("/peak/")
        assert resp.status_code == 200
        content = resp.content.decode()
        # 4-9pm: 5h * 500Wh = 2.5 kWh; off-peak: 19h * 500Wh = 9.5 kWh
        assert f"${2.5 * SUMMER_PEAK:.2f}" in content
        assert f"${9.5 * SUMMER_OFFPEAK:.2f}" in content

    def test_costmap_cell_is_real_from_green_button_only_data(
        self, tmp_path, client, monkeypatch
    ):
        from billing.rates import SUMMER_PEAK

        frozen_today = date(2026, 8, 10)
        monkeypatch.setattr("catalog.views.timezone.localdate", lambda: frozen_today)

        path = _write(
            tmp_path, "full_day.csv", _full_day_single_usage_csv("08/10/2026", hourly_kwh=0.5)
        )
        call_command("import_greenbutton", path)

        data = client.get("/costmap/data.json").json()
        di = data["days"].index("2026-08-10")
        assert data["z"][17][di] == pytest.approx(0.5 * SUMMER_PEAK)  # 5pm, inside 4-9pm


@pytest.mark.django_db
class TestGridHourlyWh:
    def _eagle_source(self):
        return Source.objects.create(
            slug="eagle",
            name="Grid (PG&E meter via Eagle 3)",
            kind=Source.Kind.GRID,
            poll_interval_s=15,
            native_resolution_s=15,
        )

    def test_prefers_demand_w_when_well_covered(self):
        source = self._eagle_source()
        demand = Series.objects.create(source=source, metric="demand_w", unit="W")
        import_series = Series.objects.create(source=source, metric="grid_import_wh", unit="Wh")

        t0 = utc(2026, 1, 1, 10, 0)
        for minute in range(60):
            Sample.objects.create(
                series=demand, ts=t0 + timedelta(minutes=minute), duration_s=60, value=1000.0
            )
        record_coverage(demand, t0, t0 + timedelta(hours=1), CoverageSpan.State.LIVE)
        # Deliberately conflicting Green Button data for the same window —
        # if this shows up in the result, the fallback path fired instead.
        Sample.objects.create(series=import_series, ts=t0, duration_s=3600, value=99999.0)
        record_coverage(import_series, t0, t0 + timedelta(hours=1), CoverageSpan.State.BACKFILLED)

        result = grid_hourly_wh(t0, t0 + timedelta(hours=1))
        assert result.imported_wh == pytest.approx(1000.0)
        assert result.coverage == pytest.approx(1.0)

    def test_falls_back_to_clipped_interval_samples_when_demand_thin(self):
        source = self._eagle_source()
        demand = Series.objects.create(source=source, metric="demand_w", unit="W")
        import_series = Series.objects.create(source=source, metric="grid_import_wh", unit="Wh")

        t0 = utc(2026, 1, 1, 10, 0)
        # Only 10 of 60 minutes of demand coverage: below the 0.99 bar.
        for minute in range(10):
            Sample.objects.create(
                series=demand, ts=t0 + timedelta(minutes=minute), duration_s=60, value=5000.0
            )
        record_coverage(demand, t0, t0 + timedelta(minutes=10), CoverageSpan.State.LIVE)

        # A 30-minute Green Button interval starting at 10:45, half inside
        # the 10:00-11:00 window: 300 Wh over 30 min -> 150 Wh clipped.
        Sample.objects.create(
            series=import_series, ts=t0 + timedelta(minutes=45), duration_s=1800, value=300.0
        )
        record_coverage(
            import_series,
            t0 + timedelta(minutes=45),
            t0 + timedelta(minutes=75),
            CoverageSpan.State.BACKFILLED,
        )

        result = grid_hourly_wh(t0, t0 + timedelta(hours=1))
        assert result.imported_wh == pytest.approx(150.0)
        assert result.exported_wh == 0.0

    def test_hourly_window_inside_a_24h_row_gets_proportional_energy(self):
        """Green Button rows can carry daily-granularity durations, unlike
        every other collector's sub-hour cadence. A fixed 1-hour lookback
        would miss a same-row sample that started long before the query
        window but still overlaps it, reading zero energy despite full
        coverage."""
        source = self._eagle_source()
        series = Series.objects.create(source=source, metric="grid_import_wh", unit="Wh")

        t0 = utc(2026, 1, 1, 0, 0)
        Sample.objects.create(series=series, ts=t0, duration_s=86400, value=2400.0)
        record_coverage(series, t0, t0 + timedelta(hours=24), CoverageSpan.State.BACKFILLED)

        window_start = t0 + timedelta(hours=2)  # 02:00-03:00, well inside the row
        window_end = t0 + timedelta(hours=3)
        result = grid_hourly_wh(window_start, window_end)
        assert result.imported_wh == pytest.approx(2400.0 * 3600 / 86400)  # 100.0, not 0
        assert result.coverage == pytest.approx(1.0)


@pytest.mark.django_db
class TestClearCoverageOnReimport:
    def test_one_hour_correction_does_not_erase_the_rest_of_the_month(self, tmp_path):
        """A one-hour correction file must only revise what it actually
        describes. The blunt "delete every BACKFILLED span the file's
        range intersects" bug nuked an entire multi-day span for a
        one-hour touch; clear_coverage scopes retraction to what's
        actually being revised. Day 3's correction lands at a DIFFERENT
        ts than the daily row it partly supersedes (05:00 vs the daily
        row's 00:00), so that whole daily reading is invalidated (it was
        one atomic value, now known partly wrong) and the rest of day 3
        reverts to unknown -- but days 1, 2, 4, and 5 are untouched."""
        month = _write(tmp_path, "month.csv", _daily_rows_csv("07/01/2026", 5, daily_kwh=24.0))
        call_command("import_greenbutton", month)

        series = Series.objects.get(metric="grid_import_wh")
        month_start = utc(2026, 7, 1, 7, 0)  # 00:00 PDT
        month_end = utc(2026, 7, 6, 7, 0)  # 5 days later
        assert uncovered(series, month_start, month_end) == []

        # Day 3's 05:00-06:00 gets corrected to a different reading.
        corrected = _write(tmp_path, "corrected_hour.csv", _single_hour_csv("07/03/2026", 5, 0.750))
        call_command("import_greenbutton", corrected)

        # Day 3's daily reading is invalidated outside the corrected hour
        # -- the other four days are untouched.
        day3_hour_start = utc(2026, 7, 3, 12, 0)  # 05:00 PDT
        day3_hour_end = utc(2026, 7, 3, 13, 0)
        assert uncovered(series, month_start, month_end) == [
            (utc(2026, 7, 3, 7, 0), day3_hour_start),
            (day3_hour_end, utc(2026, 7, 4, 7, 0)),
        ]

        # The corrected hour actually reflects the new file.
        sample = Sample.objects.get(series=series, ts=day3_hour_start)
        assert sample.value == pytest.approx(750.0)
        assert sample.duration_s == 3600

        # The old day-3 daily sample is gone -- not just shadowed.
        assert not Sample.objects.filter(series=series, ts=utc(2026, 7, 3, 7, 0)).exists()

        # A day the correction never touched keeps its original reading.
        untouched_ts = utc(2026, 7, 1, 7, 0)
        untouched = Sample.objects.get(series=series, ts=untouched_ts)
        assert untouched.value == pytest.approx(24000.0)
        assert untouched.duration_s == 86400


@pytest.mark.django_db
class TestOverlappingDifferentTsSample:
    def test_hourly_correction_inside_a_daily_row_removes_the_daily_sample(self, tmp_path):
        """The daily row's ts (00:00) differs from the corrected hour's ts
        (05:00), so update_or_create's exact-(series, ts) key can't find
        it. Without deleting the old overlapping sample, both the daily
        reading and the corrected hour would exist side by side, and
        _clipped_energy_wh would double-count: the daily sample's full
        proportional slice, plus the new hourly value, while coverage
        looked completely intact."""
        daily = _write(tmp_path, "daily.csv", DAILY_ROW_CSV)
        call_command("import_greenbutton", daily)

        hourly = _write(tmp_path, "hourly.csv", CORRECTED_HOUR_INSIDE_DAY_CSV)
        call_command("import_greenbutton", hourly)

        series = Series.objects.get(metric="grid_import_wh")
        # The daily sample is gone -- not shadowed, not double-counted.
        assert Sample.objects.filter(series=series).count() == 1
        sample = Sample.objects.get(series=series)
        assert sample.ts == utc(2025, 8, 6, 12, 0)  # 05:00 PDT
        assert sample.value == pytest.approx(2000.0)
        assert sample.duration_s == 3600

        day_start = utc(2025, 8, 6, 7, 0)  # 00:00 PDT
        day_end = utc(2025, 8, 7, 7, 0)
        hour_start = utc(2025, 8, 6, 12, 0)
        hour_end = utc(2025, 8, 6, 13, 0)

        # The remainder of the day reads unknown -- not zero, and not
        # still "known" via a sample that no longer exists.
        assert uncovered(series, day_start, day_end) == [
            (day_start, hour_start),
            (hour_end, day_end),
        ]

        # Totals only count what's actually covered: the corrected hour,
        # not the deleted daily reading it used to double-count against.
        result = grid_hourly_wh(hour_start, hour_end)
        assert result.imported_wh == pytest.approx(2000.0)
        assert result.coverage == pytest.approx(1.0)

        untouched_hour = grid_hourly_wh(day_start, day_start + timedelta(hours=1))
        assert untouched_hour.imported_wh == 0.0
        assert untouched_hour.coverage == 0.0
