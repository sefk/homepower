"""SolarEdge cloud collector — main-house solar at 15-minute resolution.

The SE6000A's bridge is a pure outbound cloud client with no local
interface (discovery doc), so v1 reads the cloud. SolarEdge would not
issue a Monitoring API key for this site (keys go through the installer
account), so this reads the SolarEdge ONE portal's own dashboard API
with the owner's portal login instead — see solaredge_auth. It is an
unpublished API: expect it to change without notice. The source layer is
built for the eventual local-Modbus swap (PRD assumption).

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
from .solaredge_auth import MONITORING_BASE, USER_AGENT

logger = logging.getLogger(__name__)

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

    def __init__(self, auth, site_id: str, session_factory=aiohttp.ClientSession):
        super().__init__()
        self.auth = auth
        self.site_id = site_id
        self._session_factory = session_factory

    async def poll(self) -> list[Reading]:
        """Fetch today's quarter-hour production means.

        The API serves whole days, so each poll re-reads the day so far:
        upsert makes that idempotent, it heals the ragged edge where the
        newest quarter was still filling in, and it backfills same-day
        downtime. Just after midnight the window reaches back into
        yesterday so its last quarters get their final values too.
        """
        now = timezone.localtime()
        params = {
            "start-date": (now - timedelta(hours=1)).date().isoformat(),
            "end-date": now.date().isoformat(),
            "chart-time-unit": "quarter-hours",
            "measurement-types": "production",
        }
        url = f"{MONITORING_BASE}/services/dashboard/power/sites/{self.site_id}"
        timeout = aiohttp.ClientTimeout(total=30)
        async with self._session_factory() as session:
            token = await self.auth.access_token(session)
            status, payload = await self._get(session, url, params, token, timeout)
            if status == 401:
                # Token revoked before its expiry; renew once and retry.
                logger.info("solaredge token rejected, re-authenticating")
                token = await self.auth.access_token(session, force=True)
                status, payload = await self._get(session, url, params, token, timeout)
        if status != 200:
            raise RuntimeError(f"solaredge API HTTP {status}")
        return self.parse_power(payload, now=now)

    async def _get(self, session, url, params, token, timeout):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Origin": MONITORING_BASE,
            "Referer": f"{MONITORING_BASE}/",
        }
        async with session.get(url, params=params, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                return resp.status, None
            return resp.status, await resp.json()

    def parse_power(self, payload: dict, now: datetime | None = None) -> list[Reading]:
        tz = timezone.get_current_timezone()
        now = now or timezone.now()
        readings = []
        for point in payload.get("measurements", []):
            # The day is served whole: quarters with no data yet (night,
            # the future, an outage) carry production null.
            if point.get("production") is None:
                continue
            # measurementTime carries its UTC offset, so the repeated
            # 01:00-01:45 hour on DST fall-back arrives as distinct instants.
            ts = datetime.fromisoformat(point["measurementTime"]).astimezone(tz)
            if ts > now:
                continue
            readings.append(
                Reading(
                    metric="production_w",
                    unit="W",
                    ts=ts,  # aware local time; Django stores UTC
                    duration_s=RESOLUTION_S,
                    value=float(point["production"]),
                )
            )
        return readings
