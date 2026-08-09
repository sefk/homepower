"""Coverage semantics — the invariant the whole system rests on:
absence of a span means unknown, and unknown never aggregates as zero.
"""

import pytest

from core import coverage
from core.aggregate import energy_wh
from core.models import CoverageSpan, Sample

from .conftest import utc

LIVE = CoverageSpan.State.LIVE
BACKFILLED = CoverageSpan.State.BACKFILLED
CONFIRMED_EMPTY = CoverageSpan.State.CONFIRMED_EMPTY


class TestRecordCoverage:
    def test_touching_same_state_spans_merge(self, series):
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 10, 1), LIVE)
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 1), utc(2026, 8, 1, 10, 2), LIVE)
        span = CoverageSpan.objects.get()  # exactly one row survives
        assert (span.start, span.end) == (utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 10, 2))

    def test_overlapping_same_state_spans_merge(self, series):
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 10, 30), LIVE)
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 15), utc(2026, 8, 1, 11, 0), LIVE)
        span = CoverageSpan.objects.get()
        assert (span.start, span.end) == (utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 11, 0))

    def test_span_swallowed_by_larger_backfill(self, series):
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 10, 5), BACKFILLED)
        coverage.record_coverage(series, utc(2026, 8, 1, 9, 0), utc(2026, 8, 1, 12, 0), BACKFILLED)
        span = CoverageSpan.objects.get()
        assert (span.start, span.end) == (utc(2026, 8, 1, 9, 0), utc(2026, 8, 1, 12, 0))

    def test_different_states_do_not_merge(self, series):
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 10, 1), LIVE)
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 1), utc(2026, 8, 1, 10, 2), BACKFILLED)
        assert CoverageSpan.objects.count() == 2

    def test_disjoint_spans_stay_separate(self, series):
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 10, 1), LIVE)
        coverage.record_coverage(series, utc(2026, 8, 1, 12, 0), utc(2026, 8, 1, 12, 1), LIVE)
        assert CoverageSpan.objects.count() == 2

    def test_empty_span_rejected(self, series):
        with pytest.raises(ValueError):
            coverage.record_coverage(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 10, 0), LIVE)


class TestGaps:
    def test_restart_gap_reads_as_unknown(self, series):
        """The collector was down 10:05–14:00; that hole must surface as a gap."""
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 10, 5), LIVE)
        coverage.record_coverage(series, utc(2026, 8, 1, 14, 0), utc(2026, 8, 1, 15, 0), LIVE)
        gaps = coverage.uncovered(series, utc(2026, 8, 1, 9, 0), utc(2026, 8, 1, 16, 0))
        assert gaps == [
            (utc(2026, 8, 1, 9, 0), utc(2026, 8, 1, 10, 0)),
            (utc(2026, 8, 1, 10, 5), utc(2026, 8, 1, 14, 0)),
            (utc(2026, 8, 1, 15, 0), utc(2026, 8, 1, 16, 0)),
        ]

    def test_confirmed_empty_is_knowledge_not_gap(self, series):
        """Vendor was asked and has nothing: healed, so no longer a gap."""
        coverage.record_coverage(series, utc(2026, 8, 1, 0, 0), utc(2026, 8, 2, 0, 0), CONFIRMED_EMPTY)
        assert coverage.uncovered(series, utc(2026, 8, 1, 0, 0), utc(2026, 8, 2, 0, 0)) == []

    def test_no_coverage_is_all_gap(self, series):
        gaps = coverage.uncovered(series, utc(2026, 8, 1, 0, 0), utc(2026, 8, 2, 0, 0))
        assert gaps == [(utc(2026, 8, 1, 0, 0), utc(2026, 8, 2, 0, 0))]

    def test_covered_fraction(self, series):
        coverage.record_coverage(series, utc(2026, 8, 1, 0, 0), utc(2026, 8, 1, 6, 0), LIVE)
        frac = coverage.covered_fraction(series, utc(2026, 8, 1, 0, 0), utc(2026, 8, 2, 0, 0))
        assert frac == pytest.approx(0.25)


class TestAggregation:
    def test_outage_month_does_not_report_as_low_usage(self, series):
        """PRD: a window with a hole must not quietly aggregate missing as zero.

        One hour of 1 kW samples, then a 1-hour hole inside a 2-hour window:
        the energy is 1000 Wh but coverage says half the window is unknown.
        """
        for minute in range(60):
            Sample.objects.create(
                series=series, ts=utc(2026, 8, 1, 10, minute), duration_s=60, value=1000.0
            )
        coverage.record_coverage(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 11, 0), LIVE)

        result = energy_wh(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 12, 0))
        assert result.wh == pytest.approx(1000.0)
        assert result.coverage == pytest.approx(0.5)  # the hole is visible, not zero-filled

    def test_straddling_sample_split_at_window_boundary(self, series):
        """A sample spanning an hour boundary charges each hour only its
        overlap — 45s at 10:59:30 puts 30s in hour 10 and 15s in hour 11."""
        Sample.objects.create(
            series=series, ts=utc(2026, 8, 1, 10, 59, 30), duration_s=45, value=1200.0
        )
        hour10 = energy_wh(series, utc(2026, 8, 1, 10, 0), utc(2026, 8, 1, 11, 0))
        hour11 = energy_wh(series, utc(2026, 8, 1, 11, 0), utc(2026, 8, 1, 12, 0))
        assert hour10.wh == pytest.approx(1200.0 * 30 / 3600)
        assert hour11.wh == pytest.approx(1200.0 * 15 / 3600)
