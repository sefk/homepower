from datetime import datetime, timezone

import pytest

from billing import rates
from core.models import Series, Source


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def series(db) -> Series:
    source = Source.objects.create(
        slug="envoy",
        name="Guest House Solar",
        kind=Source.Kind.SOLAR,
        poll_interval_s=60,
        native_resolution_s=60,
    )
    return Series.objects.create(source=source, metric="production_w", unit="W")


@pytest.fixture(autouse=True)
def fresh_rate_cache():
    """billing.rates caches rows in-process; test transactions roll back
    without telling it, so start and end every test with it empty."""
    rates.clear_cache()
    yield
    rates.clear_cache()


@pytest.fixture
def no_winter_adjustment(db):
    """Undo the April 2026 bill's winter CCA adjustment (migration 0004),
    recreating the gap missing_adjustments() exists to flag."""
    from datetime import date

    from billing.models import CcaAdjustment

    CcaAdjustment.objects.filter(season="winter", effective_from__gte=date(2026, 3, 1)).delete()
