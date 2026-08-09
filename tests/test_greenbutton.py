"""Green Button CSV import: header scanning, column variants, duration
inference, DST fold, idempotency, coverage, and grid_hourly_wh."""

from datetime import timedelta

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


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return str(path)


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
