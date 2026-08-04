"""Enphase Envoy collector — ADU solar, local API at 1-minute resolution.

Firmware D7+ requires a JWT minted from Enlighten cloud credentials
before the local API answers (discovery doc). The token is cached in
var/ across restarts and refreshed via re-authentication when the Envoy
rejects it, so the Enlighten round-trip happens rarely — but token
refresh is a standing requirement, not one-time setup.
"""

import json
import logging
from pathlib import Path

from django.utils import timezone
from pyenphase import Envoy, EnvoyAuthenticationError, EnvoyAuthenticationRequired

from core.models import Source

from .base import Collector, Reading

logger = logging.getLogger(__name__)


class EnvoyCollector(Collector):
    slug = "envoy"
    name = "ADU solar (Enphase Envoy)"
    kind = Source.Kind.SOLAR
    poll_interval_s = 60
    native_resolution_s = 60

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        token_file: Path,
        envoy_factory=Envoy,
    ):
        super().__init__()
        self.host = host
        self.username = username
        self.password = password
        self.token_file = Path(token_file)
        self._envoy_factory = envoy_factory
        self._envoy = None

    async def setup(self) -> None:
        await super().setup()
        self._envoy = self._envoy_factory(self.host)
        await self._envoy.setup()
        await self._authenticate()

    async def poll(self) -> list[Reading]:
        try:
            data = await self._envoy.update()
        except (EnvoyAuthenticationError, EnvoyAuthenticationRequired):
            # Cached token went stale mid-run; re-mint once and retry.
            logger.info("envoy token rejected, re-authenticating")
            await self._authenticate(force_cloud=True)
            data = await self._envoy.update()

        ts = timezone.now().replace(microsecond=0)
        readings = [
            Reading(
                metric="production_w",
                unit="W",
                ts=ts,
                duration_s=self.native_resolution_s,
                value=float(data.system_production.watts_now),
            )
        ]
        # Present only if consumption CTs are installed (imeter reports them).
        if data.system_consumption is not None:
            readings.append(
                Reading(
                    metric="consumption_w",
                    unit="W",
                    ts=ts,
                    duration_s=self.native_resolution_s,
                    value=float(data.system_consumption.watts_now),
                )
            )
        return readings

    async def _authenticate(self, force_cloud: bool = False) -> None:
        token = None if force_cloud else self._load_token()
        if token:
            try:
                await self._envoy.authenticate(token=token)
                return
            except (EnvoyAuthenticationError, EnvoyAuthenticationRequired):
                logger.info("cached envoy token invalid, falling back to Enlighten")
        await self._envoy.authenticate(username=self.username, password=self.password)
        self._save_token(self._envoy.auth.token)

    def _load_token(self) -> str | None:
        try:
            return json.loads(self.token_file.read_text())["token"]
        except (OSError, KeyError, ValueError):
            return None

    def _save_token(self, token: str) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps({"token": token}))
        logger.info("saved envoy token to %s", self.token_file)
