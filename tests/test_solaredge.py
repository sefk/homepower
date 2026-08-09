"""SolarEdge cloud collector: powerDetails parsing, polling, registry gating."""

import asyncio

import pytest
from django.utils import timezone

from collectors.registry import enabled_collectors
from collectors.solaredge import SolarEdgeCollector
from core.models import CollectorRun, CoverageSpan, Sample, Source

# Realistic powerDetails response shape. The last quarter is still live at
# the vendor, so it has no "value" yet — the collector must skip it, not
# error, and pick it up on a later poll once it finalizes.
POWER_DETAILS = {
    "powerDetails": {
        "timeUnit": "QUARTER_OF_AN_HOUR",
        "unit": "W",
        "meters": [
            {
                "type": "Production",
                "values": [
                    {"date": "2026-08-01 10:00:00", "value": 4521.3},
                    {"date": "2026-08-01 10:15:00", "value": 4600.0},
                    {"date": "2026-08-01 10:30:00"},
                ],
            }
        ],
    }
}


class TestParsePowerDetails:
    def test_skips_quarter_without_value(self):
        collector = SolarEdgeCollector(api_key="k", site_id="1")
        readings = collector.parse_power_details(POWER_DETAILS)
        assert len(readings) == 2

    def test_reading_fields(self):
        collector = SolarEdgeCollector(api_key="k", site_id="1")
        readings = collector.parse_power_details(POWER_DETAILS)
        r = readings[0]
        assert r.metric == "production_w"
        assert r.unit == "W"
        assert r.value == 4521.3
        assert r.duration_s == 900

    def test_timestamps_are_aware_in_local_time(self):
        collector = SolarEdgeCollector(api_key="k", site_id="1")
        readings = collector.parse_power_details(POWER_DETAILS)
        ts = readings[0].ts
        assert timezone.is_aware(ts)
        assert ts.tzinfo == timezone.get_current_timezone()
        assert (ts.hour, ts.minute) == (10, 0)


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
    """Stands in for aiohttp.ClientSession: records the request, serves canned JSON."""

    def __init__(self, status=200, payload=None, calls=None):
        self.status = status
        self.payload = payload
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return FakeResponse(self.status, self.payload)


class TestPoll:
    def test_poll_returns_parsed_readings(self):
        calls = []
        collector = SolarEdgeCollector(
            api_key="k",
            site_id="1",
            session_factory=lambda: FakeSession(payload=POWER_DETAILS, calls=calls),
        )
        readings = asyncio.run(collector.poll())
        assert len(readings) == 2
        assert calls[0]["params"]["api_key"] == "k"
        assert calls[0]["params"]["meters"] == "Production"
        assert "1" in calls[0]["url"]

    def test_poll_raises_on_non_200(self):
        collector = SolarEdgeCollector(
            api_key="k",
            site_id="1",
            session_factory=lambda: FakeSession(status=500, payload={}),
        )
        with pytest.raises(RuntimeError):
            asyncio.run(collector.poll())


class TestStorage:
    def test_run_once_writes_samples_and_live_coverage(self, transactional_db):
        collector = SolarEdgeCollector(
            api_key="k",
            site_id="1",
            session_factory=lambda: FakeSession(payload=POWER_DETAILS),
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
        settings.SOLAREDGE_API_KEY = ""
        settings.SOLAREDGE_SITE_ID = ""
        settings.ENPHASE_USERNAME = ""
        settings.ENPHASE_PASSWORD = ""
        with caplog.at_level("WARNING"):
            collectors = enabled_collectors()
        assert not any(isinstance(c, SolarEdgeCollector) for c in collectors)
        assert "solaredge" in caplog.text

    def test_enabled_with_credentials(self, settings):
        settings.SOLAREDGE_API_KEY = "key123"
        settings.SOLAREDGE_SITE_ID = "9999"
        settings.ENPHASE_USERNAME = ""
        settings.ENPHASE_PASSWORD = ""
        collectors = enabled_collectors()
        matches = [c for c in collectors if isinstance(c, SolarEdgeCollector)]
        assert len(matches) == 1
        assert matches[0].api_key == "key123"
        assert matches[0].site_id == "9999"


class TestOmittedQuarterCoverage:
    def test_missing_middle_quarter_stays_unknown(self, transactional_db):
        """An omitted vendor quarter must not be bridged into live coverage —
        the hourly table would claim 100% while its Wh total excludes it."""
        import asyncio
        from datetime import timedelta

        from django.utils import timezone as djtz

        from core.models import CoverageSpan

        payload = {
            "powerDetails": {
                "meters": [
                    {
                        "type": "Production",
                        "values": [
                            {"date": "2026-08-01 10:00:00", "value": 4000.0},
                            # 10:15 omitted by the vendor
                            {"date": "2026-08-01 10:30:00", "value": 4200.0},
                        ],
                    }
                ]
            }
        }

        collector = SolarEdgeCollector(api_key="k", site_id="1")

        async def _go():
            await collector.setup()
            collector.poll = lambda: _readings()  # bypass HTTP

            async def _readings():
                return collector.parse_power_details(payload)

            collector.poll = _readings
            await collector.run_once()

        asyncio.run(_go())
        spans = CoverageSpan.objects.order_by("start")
        assert spans.count() == 2  # the omitted quarter is a hole
        assert spans[0].end + timedelta(seconds=900) == spans[1].start
