"""Tesla Fleet API collector — EV charging power, home-charging detection.

Auth is a stored refresh token (minted once via `manage.py tesla_auth`;
see ops/tesla-setup.md for the developer-app registration and public-key
hosting steps this requires). The Fleet API rotates the refresh token on
every use, so — mirroring the Envoy JWT cache in collectors/envoy.py —
the rotated token is persisted to var/tesla_token.json after every poll;
a process restart picks up the latest token instead of needing the
manual auth flow re-run.

tesla-fleet-api's own TeslaFleetError subclasses BaseException, not
Exception (a choice made upstream, not here). Collector.run_once() in
collectors/base.py only catches `except Exception`, so every vendor
call in poll() must be wrapped and re-raised as an ordinary exception —
otherwise a sleeping/unreachable car (the routine case; a 408 from the
vendor) would escape run_once's failure bookkeeping instead of
recording a failed run and leaving coverage unknown.
"""

import json
import logging
import math
from pathlib import Path

import aiohttp
from django.utils import timezone
from tesla_fleet_api import TeslaFleetOAuth
from tesla_fleet_api.exceptions import TeslaFleetError

from core.models import Source

from .base import Collector, Reading

logger = logging.getLogger(__name__)

# Best-effort home-charging geofence (PRD wants home charging cost, not
# a global one). ~200m; a great-circle distance, not a precise fence.
HOME_RADIUS_M = 200.0
EARTH_RADIUS_M = 6371000.0


def _default_client_factory(
    session: aiohttp.ClientSession,
    region: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> TeslaFleetOAuth:
    return TeslaFleetOAuth(
        session=session,
        region=region,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class TeslaCollector(Collector):
    slug = "tesla"
    name = "EV (Tesla Model S)"
    kind = Source.Kind.EV
    poll_interval_s = 60
    native_resolution_s = 60

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_file: Path,
        vin: str | None = None,
        region: str = "na",
        home_lat: float | None = None,
        home_lon: float | None = None,
        client_factory=_default_client_factory,
        session_factory=aiohttp.ClientSession,
    ):
        super().__init__()
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.token_file = Path(token_file)
        self.vin = vin
        self.region = region
        self.home_lat = home_lat
        self.home_lon = home_lon
        self._client_factory = client_factory
        self._session_factory = session_factory
        self._session = None
        self._client = None

    async def setup(self) -> None:
        await super().setup()
        self._session = self._session_factory()
        token = self._load_refresh_token() or self.refresh_token
        self._client = self._client_factory(
            self._session, self.region, self.client_id, self.client_secret, token
        )
        if self.vin is None:
            self.vin = await self._discover_vin()

    async def _discover_vin(self) -> str:
        # PRD assumption: the free Fleet API tier covers one vehicle at
        # this poll rate, so the account's first vehicle is the car.
        try:
            data = await self._client.vehicles.list()
        except TeslaFleetError as exc:
            raise RuntimeError(f"tesla: could not list vehicles: {exc}") from exc
        vehicles = data.get("response") or []
        if not vehicles:
            raise RuntimeError("tesla: account has no vehicles")
        return vehicles[0]["vin"]

    async def poll(self) -> list[Reading]:
        try:
            data = await self._client.vehicles.createFleet(self.vin).vehicle_data(
                ["charge_state", "drive_state"]
            )
        except TeslaFleetError as exc:
            # The common case is a sleeping/unreachable car (vendor 408).
            # Either way: no reading. Absence must read as unknown, never
            # a synthesized zero, so this is a failed run, not a sample.
            raise RuntimeError(f"tesla: vehicle unreachable: {exc}") from exc
        finally:
            self._persist_rotated_token()

        response = data.get("response") or {}
        charge_state = response.get("charge_state")
        if charge_state is None:
            # Woken but the vendor didn't return charge data -- same
            # "unknown" outcome as an unreachable car.
            raise RuntimeError("tesla: vehicle_data returned no charge_state")

        ts = timezone.now().replace(microsecond=0)
        charging = charge_state.get("charging_state") == "Charging"
        if charging and self._is_home(response.get("drive_state")):
            amps = charge_state.get("charger_actual_current") or 0.0
            volts = charge_state.get("charger_voltage") or 0.0
            power_w = float(amps) * float(volts)
        else:
            # An awake car that isn't charging -- or is charging away from
            # home (a Supercharger stop, not a home-cost data point) -- is
            # a REAL zero here, not an absence. charge_state exists, so
            # this instant IS known; that's the coverage model's whole
            # point (PRD: absence means unknown, never zero -- and the
            # converse holds too, a known zero must not be hidden as a gap).
            power_w = 0.0

        return [
            Reading(
                metric="ev_charge_power_w",
                unit="W",
                ts=ts,
                duration_s=self.native_resolution_s,
                value=power_w,
            )
        ]

    def _is_home(self, drive_state: dict | None) -> bool:
        """Best-effort home-charging geofence.

        Home detection only ever suppresses a charging reading to 0 W
        when location is confidently away; any uncertainty (no
        drive_state, location sharing off, a missed GPS fix) defaults to
        "home" so a transient vendor omission never drops real home
        charging data -- the PRD wants home charging cost measured, and
        undercounting it silently would be worse than the rare
        Supercharger stop miscounted as home.
        """
        if not drive_state or self.home_lat is None or self.home_lon is None:
            return True
        lat, lon = drive_state.get("latitude"), drive_state.get("longitude")
        if lat is None or lon is None:
            return True
        return _haversine_m(lat, lon, self.home_lat, self.home_lon) <= HOME_RADIUS_M

    def _load_refresh_token(self) -> str | None:
        try:
            return json.loads(self.token_file.read_text())["refresh_token"]
        except (OSError, KeyError, ValueError):
            return None

    def _persist_rotated_token(self) -> None:
        token = getattr(self._client, "refresh_token", None)
        if not token or token == self.refresh_token:
            return
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps({"refresh_token": token}))
        self.refresh_token = token
        logger.info("saved rotated tesla refresh token to %s", self.token_file)
