"""Tesla Fleet API collector — EV charging power and energy, cost-capped.

Auth is a stored refresh token (minted once via `manage.py tesla_auth`;
see ops/tesla-setup.md for the developer-app registration and public-key
hosting steps this requires). The Fleet API rotates the refresh token on
every use, so — mirroring the Envoy JWT cache in collectors/envoy.py —
the rotated token is persisted to var/tesla_token.json after every poll;
a process restart picks up the latest token instead of needing the
manual auth flow re-run.

The Fleet API bills per request (even a sleeping car's 408), with a $10
monthly credit, so polling is shaped to stay inside it:

- Idle: every IDLE_INTERVAL_S, one cheap state check (GET
  /vehicles/{vin}), which never wakes the car. Asleep means not
  charging — a charging car doesn't sleep — so that's a known 0 W.
- Online: one vehicle_data read (charge_state only; never a wake_up).
- Charging: vehicle_data every CHARGING_INTERVAL_S until it stops,
  skipping the state check.
- Every Fleet API call counts against a monthly budget persisted in
  var/; once spent, polls fail without touching the API until the next
  calendar month.

Power readings are snapshots held until the next poll, so a session's
start can lag by up to one idle interval. Energy doesn't: each
vehicle_data read also emits ev_charge_energy_wh, the change in the
car's charge_energy_added counter since the previous read, covering
exactly that interval — sessions total correctly at any cadence.

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
from datetime import datetime
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

IDLE_INTERVAL_S = 1800
CHARGING_INTERVAL_S = 300
# ~100 requests/day expected; this is the runaway-loop backstop, set
# below the $10 credit's ~5,000 data requests.
DEFAULT_MONTHLY_BUDGET = 4000


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


class BudgetExhausted(RuntimeError):
    pass


class RequestBudget:
    """Fleet API calls made this calendar month (local time), on disk so
    a restart can't reset the count."""

    def __init__(self, path: Path, limit: int):
        self.path = Path(path)
        self.limit = limit

    def _month(self) -> str:
        return timezone.localtime().strftime("%Y-%m")

    def used(self) -> int:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return 0
        return data.get("requests", 0) if data.get("month") == self._month() else 0

    def spend(self) -> None:
        used = self.used()
        if used >= self.limit:
            raise BudgetExhausted(
                f"tesla: monthly request budget spent ({used}/{self.limit}); "
                "polling resumes next month"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"month": self._month(), "requests": used + 1}))


class TeslaCollector(Collector):
    slug = "tesla"
    name = "Tesla"
    kind = Source.Kind.EV
    # The slow cadence: grace for bridging coverage is a multiple of it,
    # so idle checks bridge and charging polls (shorter) do too.
    poll_interval_s = IDLE_INTERVAL_S
    native_resolution_s = CHARGING_INTERVAL_S

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_file: Path,
        usage_file: Path,
        monthly_budget: int = DEFAULT_MONTHLY_BUDGET,
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
        self.budget = RequestBudget(usage_file, monthly_budget)
        self.vin = vin
        self.region = region
        self.home_lat = home_lat
        self.home_lon = home_lon
        self._client_factory = client_factory
        self._session_factory = session_factory
        self._session = None
        self._client = None
        self._charging = False
        # charge_energy_added baseline: (kWh, read time). None until the
        # first vehicle_data read after start-up.
        self._energy: tuple[float, datetime] | None = None
        # Unplugged since the baseline: the counter restarts with the
        # next session, so its next value is all new energy.
        self._new_session = False

    def next_interval_s(self) -> int:
        return CHARGING_INTERVAL_S if self._charging else IDLE_INTERVAL_S

    async def setup(self) -> None:
        await super().setup()
        self._session = self._session_factory()
        token = self._load_refresh_token() or self.refresh_token
        self._client = self._client_factory(
            self._session, self.region, self.client_id, self.client_secret, token
        )
        if self.vin is None:
            self.vin = await self._discover_vin()

    async def _call(self, what: str, coro_fn):
        """One billed Fleet API request: budget first, then the call."""
        self.budget.spend()
        try:
            return await coro_fn()
        except TeslaFleetError as exc:
            raise RuntimeError(f"tesla: {what}: {exc}") from exc
        finally:
            self._persist_rotated_token()

    async def _discover_vin(self) -> str:
        # One car on the account (the request budget assumes as much),
        # so its first vehicle is the car.
        data = await self._call("could not list vehicles", self._client.products)
        # Products are vehicles and energy sites; only vehicles have a VIN.
        vehicles = [p for p in data.get("response") or [] if p.get("vin")]
        if not vehicles:
            raise RuntimeError("tesla: account has no vehicles")
        return vehicles[0]["vin"]

    async def poll(self) -> list[Reading]:
        vehicle = self._client.vehicles.createFleet(self.vin)
        ts = timezone.now().replace(microsecond=0)

        if not self._charging:
            info = await self._call("vehicle state", vehicle.vehicle)
            state = (info.get("response") or {}).get("state")
            if state == "asleep":
                return [self._power(ts, 0.0)]
            if state != "online":
                # "offline": no connectivity, so nothing is known.
                raise RuntimeError(f"tesla: vehicle {state or 'state unknown'}")

        endpoints = ["charge_state"]
        if self.home_lat is not None and self.home_lon is not None:
            # Firmware 2023.38+ only returns coordinates via location_data,
            # which needs the app's vehicle_location scope.
            endpoints.append("location_data")
        try:
            data = await self._call("vehicle_data", lambda: vehicle.vehicle_data(endpoints))
        except RuntimeError:
            # Went to sleep between the state check and the read, or
            # mid-session: back to cheap state checks.
            self._charging = False
            raise

        response = data.get("response") or {}
        charge_state = response.get("charge_state")
        if charge_state is None:
            # Woken but the vendor didn't return charge data -- same
            # "unknown" outcome as an unreachable car.
            raise RuntimeError("tesla: vehicle_data returned no charge_state")

        state = charge_state.get("charging_state")
        self._charging = state == "Charging"
        at_home = not charge_state.get("fast_charger_present") and self._is_home(
            response.get("drive_state")
        )
        if self._charging and at_home:
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

        readings = [self._power(ts, power_w)]
        energy = self._energy_reading(ts, charge_state, at_home)
        if energy is not None:
            readings.append(energy)
        return readings

    def _power(self, ts: datetime, value: float) -> Reading:
        # Held until the next poll, whose timing this poll just decided.
        return Reading(
            metric="ev_charge_power_w",
            unit="W",
            ts=ts,
            duration_s=self.next_interval_s(),
            value=value,
        )

    def _energy_reading(self, ts: datetime, charge_state: dict, at_home: bool) -> Reading | None:
        kwh = charge_state.get("charge_energy_added")
        if kwh is None:
            return None
        kwh = float(kwh)
        prev = self._energy
        self._energy = (kwh, ts)
        unplugged = charge_state.get("charging_state") == "Disconnected"
        if prev is None:
            self._new_session = unplugged
            return None
        prev_kwh, prev_ts = prev
        if unplugged:
            added = 0.0
            self._new_session = True
        elif self._new_session and (self._charging or kwh < prev_kwh):
            # Plugged in again and the counter has restarted.
            added = kwh
            self._new_session = False
        elif self._new_session:
            # Plugged in but not charging yet: counter may still hold the
            # last session's total. Wait for it to restart.
            added = 0.0
        else:
            added = kwh - prev_kwh if kwh >= prev_kwh else kwh
        return Reading(
            metric="ev_charge_energy_wh",
            unit="Wh",
            ts=prev_ts,
            duration_s=int((ts - prev_ts).total_seconds()),
            value=added * 1000 if at_home else 0.0,
        )

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
