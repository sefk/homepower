"""SolarEdge cloud collector: dashboard power parsing, polling, registry gating."""

import asyncio
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.utils import timezone

from collectors.registry import enabled_collectors
from collectors.solaredge import SolarEdgeCollector
from core.models import CollectorRun, CoverageSpan, Sample, Source

# "now" for parsing: after every quarter in the fixtures below.
NOW = datetime(2026, 12, 1, tzinfo=dt_timezone.utc)


def point(ts, production):
    """One measurement in the portal's shape (other fields are null for
    a production-only site and are ignored)."""
    return {
        "measurementTime": ts,
        "production": production,
        "consumption": None,
        "weatherDescription": "Sunny",
    }


# Realistic dashboard power response shape. The portal serves whole days,
# so quarters with no data yet carry production null — the collector must
# skip them, not error, and pick them up on a later poll.
POWER = {
    "measurements": [
        point("2026-08-01T10:00:00-07:00", 4521.3),
        point("2026-08-01T10:15:00-07:00", 4600.0),
        point("2026-08-01T10:30:00-07:00", None),
    ]
}


class FakeAuth:
    """Stands in for SolarEdgeAuth: hands out tokens, records forced renewals."""

    def __init__(self):
        self.forced = 0

    async def access_token(self, session, force=False):
        if force:
            self.forced += 1
            return "renewed"
        return "tok"


def make_collector(**kwargs):
    return SolarEdgeCollector(auth=FakeAuth(), site_id="1", **kwargs)


class TestParsePower:
    def test_skips_quarter_without_value(self):
        readings = make_collector().parse_power(POWER, now=NOW)
        assert len(readings) == 2

    def test_reading_fields(self):
        r = make_collector().parse_power(POWER, now=NOW)[0]
        assert r.metric == "production_w"
        assert r.unit == "W"
        assert r.value == 4521.3
        assert r.duration_s == 900

    def test_timestamps_are_aware_in_local_time(self):
        ts = make_collector().parse_power(POWER, now=NOW)[0].ts
        assert timezone.is_aware(ts)
        assert ts.tzinfo == timezone.get_current_timezone()
        assert (ts.hour, ts.minute) == (10, 0)

    def test_skips_quarters_after_now(self):
        now = datetime.fromisoformat("2026-08-01T10:10:00-07:00")
        readings = make_collector().parse_power(POWER, now=now)
        assert len(readings) == 1


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class FakeSession:
    """Stands in for aiohttp.ClientSession: records requests, serves canned
    JSON. `statuses` scripts successive responses; the last one repeats."""

    def __init__(self, statuses=(200,), payload=None, calls=None):
        self.statuses = list(statuses)
        self.payload = payload
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return FakeResponse(status, self.payload)


class TestPoll:
    def test_poll_returns_parsed_readings(self):
        calls = []
        collector = make_collector(
            session_factory=lambda: FakeSession(payload=POWER, calls=calls),
        )
        readings = asyncio.run(collector.poll())
        assert len(readings) == 2
        assert calls[0]["url"].endswith("/services/dashboard/power/sites/1")
        assert calls[0]["params"]["chart-time-unit"] == "quarter-hours"
        assert calls[0]["params"]["measurement-types"] == "production"
        assert calls[0]["headers"]["Authorization"] == "Bearer tok"

    def test_poll_raises_on_non_200(self):
        collector = make_collector(
            session_factory=lambda: FakeSession(statuses=[500], payload={}),
        )
        with pytest.raises(RuntimeError):
            asyncio.run(collector.poll())

    def test_rejected_token_is_renewed_once_and_retried(self):
        calls = []
        collector = make_collector(
            session_factory=lambda: FakeSession(
                statuses=[401, 200], payload=POWER, calls=calls
            ),
        )
        readings = asyncio.run(collector.poll())
        assert len(readings) == 2
        assert collector.auth.forced == 1
        assert calls[1]["headers"]["Authorization"] == "Bearer renewed"

    def test_persistent_401_raises(self):
        collector = make_collector(
            session_factory=lambda: FakeSession(statuses=[401], payload={}),
        )
        with pytest.raises(RuntimeError):
            asyncio.run(collector.poll())
        assert collector.auth.forced == 1


class TestStorage:
    def test_run_once_writes_samples_and_live_coverage(self, transactional_db):
        collector = make_collector(
            session_factory=lambda: FakeSession(payload=POWER),
        )

        async def _go():
            await collector.setup()
            await collector.run_once()

        asyncio.run(_go())

        assert Sample.objects.count() == 2
        assert CoverageSpan.objects.get().state == CoverageSpan.State.LIVE
        assert CollectorRun.objects.get().ok is True
        assert Source.objects.get(slug="solaredge").native_resolution_s == 900


class TestRegistry:
    def test_disabled_without_credentials(self, settings, caplog):
        settings.SOLAREDGE_USERNAME = ""
        settings.SOLAREDGE_PASSWORD = ""
        settings.SOLAREDGE_SITE_ID = ""
        settings.ENPHASE_USERNAME = ""
        settings.ENPHASE_PASSWORD = ""
        with caplog.at_level("WARNING"):
            collectors = enabled_collectors()
        assert not any(isinstance(c, SolarEdgeCollector) for c in collectors)
        assert "solaredge" in caplog.text

    def test_enabled_with_credentials(self, settings, tmp_path):
        settings.SOLAREDGE_USERNAME = "me@example.com"
        settings.SOLAREDGE_PASSWORD = "pw"
        settings.SOLAREDGE_SITE_ID = "9999"
        settings.SOLAREDGE_TOKEN_FILE = tmp_path / "solaredge_token.json"
        settings.ENPHASE_USERNAME = ""
        settings.ENPHASE_PASSWORD = ""
        collectors = enabled_collectors()
        matches = [c for c in collectors if isinstance(c, SolarEdgeCollector)]
        assert len(matches) == 1
        assert matches[0].auth.username == "me@example.com"
        assert matches[0].site_id == "9999"


class TestOmittedQuarterCoverage:
    def test_missing_middle_quarter_stays_unknown(self, transactional_db):
        """An omitted vendor quarter must not be bridged into live coverage —
        the hourly table would claim 100% while its Wh total excludes it."""
        payload = {
            "measurements": [
                point("2026-08-01T10:00:00-07:00", 4000.0),
                point("2026-08-01T10:15:00-07:00", None),  # vendor has no data
                point("2026-08-01T10:30:00-07:00", 4200.0),
            ]
        }

        collector = make_collector()

        async def _go():
            await collector.setup()

            async def _readings():
                return collector.parse_power(payload, now=NOW)

            collector.poll = _readings  # bypass HTTP
            await collector.run_once()

        asyncio.run(_go())
        spans = CoverageSpan.objects.order_by("start")
        assert spans.count() == 2  # the omitted quarter is a hole
        assert spans[0].end + timedelta(seconds=900) == spans[1].start


class TestDstFallback:
    def test_repeated_local_hour_yields_distinct_instants(self):
        """Fall-back Sunday: 01:00-01:45 appears twice in local time. Both
        passes must survive as distinct UTC instants — losing an hour of
        production to the (series, ts) upsert is a silent annual bug. The
        portal's timestamps carry their offset, which is what keeps them
        apart."""
        stamps = [
            "2026-11-01T00:45:00-07:00",
            "2026-11-01T01:00:00-07:00",  # PDT pass
            "2026-11-01T01:15:00-07:00",
            "2026-11-01T01:30:00-07:00",
            "2026-11-01T01:45:00-07:00",
            "2026-11-01T01:00:00-08:00",  # PST pass
            "2026-11-01T01:15:00-08:00",
            "2026-11-01T01:30:00-08:00",
            "2026-11-01T01:45:00-08:00",
            "2026-11-01T02:00:00-08:00",
        ]
        payload = {"measurements": [point(ts, float(i)) for i, ts in enumerate(stamps)]}
        readings = make_collector().parse_power(payload, now=NOW)
        assert len(readings) == 10
        utcs = [r.ts.astimezone(dt_timezone.utc) for r in readings]
        assert len(set(utcs)) == 10  # no collisions, nothing overwritten
        assert utcs == sorted(utcs)  # strictly chronological in UTC
