"""SolarEdge cloud collector — main-house solar at 15-minute resolution.

The SE6000A's bridge is a pure outbound cloud client with no local
interface (discovery doc), so v1 reads the Monitoring API. Budget: 300
requests/day; polling powerDetails every 15 minutes uses 96. The source
layer is built for the eventual local-Modbus swap (PRD assumption).

The API returns quarter-hour mean power values. Samples are stored at
that native 900s resolution — the UI renders them stepped, never
smoothed (PRD: mixed resolution is permanent).
"""

import logging
from datetime import datetime, timedelta

import aiohttp
from django.utils import timezone

from core.models import Source

from .base import Collector, Reading

logger = logging.getLogger(__name__)

API_BASE = "https://monitoringapi.solaredge.com"
RESOLUTION_S = 900


class SolarEdgeCollector(Collector):
    slug = "solaredge"
    name = "Main solar (SolarEdge cloud)"
    kind = Source.Kind.SOLAR
    poll_interval_s = RESOLUTION_S
    native_resolution_s = RESOLUTION_S

    @property
    def grace_s(self) -> int:
        # Quarters are an interval series: only exactly-adjacent readings
        # bridge. An omitted quarter must stay unknown, not get covered by
        # the poller-jitter grace.
        return RESOLUTION_S

    def __init__(self, api_key: str, site_id: str, session_factory=aiohttp.ClientSession):
        super().__init__()
        self.api_key = api_key
        self.site_id = site_id
        self._session_factory = session_factory

    async def poll(self) -> list[Reading]:
        """Fetch the last hour of quarter-hour production means.

        An hour's window means each poll re-reads a few recent quarters:
        upsert makes that idempotent, and it heals the ragged edge where
        the API hadn't yet finalized the newest quarter.
        """
        now = timezone.localtime()
        start = now - timedelta(hours=1)
        params = {
            "api_key": self.api_key,
            "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "meters": "Production",
        }
        url = f"{API_BASE}/site/{self.site_id}/powerDetails"
        async with self._session_factory() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"solaredge API HTTP {resp.status}")
                payload = await resp.json()
        return self.parse_power_details(payload)

    def parse_power_details(self, payload: dict) -> list[Reading]:
        tz = timezone.get_current_timezone()
        readings = []
        for meter in payload.get("powerDetails", {}).get("meters", []):
            if meter.get("type") != "Production":
                continue
            for point in meter.get("values", []):
                if "value" not in point:
                    continue  # API omits value for not-yet-final quarters
                ts = datetime.strptime(point["date"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=tz
                )
                readings.append(
                    Reading(
                        metric="production_w",
                        unit="W",
                        ts=ts,  # aware local time; Django stores UTC
                        duration_s=RESOLUTION_S,
                        value=float(point["value"]),
                    )
                )
        return readings
