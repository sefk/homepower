from datetime import datetime, timezone

import pytest

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
