"""Navien NWP500 collector — the heat pump water heater's power draw.

Navien publishes no API; nwp500-python speaks the NaviLink app's own
protocol, signing in with the NaviLink email and password (unofficial,
like the SolarEdge portal login). Live status arrives over AWS IoT MQTT,
not REST, so setup() opens one connection and keeps it; each poll asks
the unit for a fresh status and waits for the reply.

currentInstPower is the whole unit's draw in watts, resistance elements
included — the library's maintainer confirmed that on a 240 V unit
(element-only ~5.1 kW, compressor plus element ~5.5 kW). A snapshot,
held for the minute like the Envoy's watts_now.
"""

import asyncio
import logging

from django.utils import timezone
from nwp500 import NavienAPIClient, NavienAuthClient, NavienMqttClient

from core.models import Source

from .base import Collector, Reading

logger = logging.getLogger(__name__)

STATUS_TIMEOUT_S = 20


async def _default_connect(username: str, password: str):
    """Sign in, find the water heater, open MQTT. Returns (auth, device, mqtt)."""
    auth = NavienAuthClient(username, password)
    await auth.__aenter__()
    try:
        device = await NavienAPIClient(auth).get_first_device()
        if device is None:
            raise RuntimeError("navien: no device on this NaviLink account")
        mqtt = NavienMqttClient(auth)
        await mqtt.connect()
    except BaseException:
        await auth.close()
        raise
    return auth, device, mqtt


class NavienCollector(Collector):
    slug = "navien"
    name = "Water Heater"
    kind = Source.Kind.DEVICE
    poll_interval_s = 60
    native_resolution_s = 60

    def __init__(self, username: str, password: str, connect=_default_connect):
        super().__init__()
        self.username = username
        self.password = password
        self._connect = connect
        self._auth = None
        self._device = None
        self._mqtt = None
        self._loop = None
        self._pending: asyncio.Future | None = None

    async def setup(self) -> None:
        await super().setup()
        self._auth, self._device, self._mqtt = await self._connect(
            self.username, self.password
        )
        await self._mqtt.subscribe_device_status(self._device, self._on_status)

    async def poll(self) -> list[Reading]:
        ts = timezone.now().replace(microsecond=0)
        self._loop = asyncio.get_running_loop()
        self._pending = self._loop.create_future()
        try:
            await self._mqtt.request_device_status(self._device)
            status = await asyncio.wait_for(self._pending, STATUS_TIMEOUT_S)
        except TimeoutError:
            raise RuntimeError(f"navien: no status reply within {STATUS_TIMEOUT_S}s")
        finally:
            self._pending = None
        return [
            Reading(
                metric="power_w",
                unit="W",
                ts=ts,
                duration_s=self.native_resolution_s,
                value=float(status.current_inst_power),
            )
        ]

    def _on_status(self, status) -> None:
        # The MQTT client may call back from its own thread.
        pending = self._pending
        if pending is not None:
            self._loop.call_soon_threadsafe(
                lambda: pending.done() or pending.set_result(status)
            )
