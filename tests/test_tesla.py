"""Tesla Fleet API collector: reading conversion, home-charging geofence,
refresh-token persistence, and registry gating.

Fakes the tesla-fleet-api client at the object level (session/region/
client_id/client_secret/refresh_token in, vehicles.list()/
vehicles.createFleet(vin).vehicle_data() out) so these tests exercise
our side only -- what becomes a Reading, and how a real
BaseException-subclassing vendor error (tesla-fleet-api's own choice,
not this codebase's) still surfaces as an ordinary failed run.
"""

import asyncio
import json

import pytest
from tesla_fleet_api.exceptions import VehicleOffline

from collectors.registry import enabled_collectors
from collectors.tesla import TeslaCollector
from core.models import CollectorRun, CoverageSpan, Sample, Source

HOME_LAT, HOME_LON = 37.4419, -122.1430
# ~1.2km from HOME_LAT/HOME_LON -- well outside the 200m geofence.
AWAY_LAT, AWAY_LON = 37.4530, -122.1430


def charge_state(charging=True, current=32.0, voltage=240.0):
    return {
        "charging_state": "Charging" if charging else "Disconnected",
        "charger_actual_current": current,
        "charger_voltage": voltage,
    }


def drive_state(lat=HOME_LAT, lon=HOME_LON):
    return {"latitude": lat, "longitude": lon}


class FakeVehicle:
    def __init__(self, client, vin):
        self._client = client
        self.vin = vin

    async def vehicle_data(self, endpoints=None):
        if self._client.vehicle_data_errors:
            raise self._client.vehicle_data_errors.pop(0)
        self._client.rotate_refresh_token()
        return {"response": self._client.data}


class FakeVehicles(dict):
    def __init__(self, client):
        super().__init__()
        self._client = client

    def createFleet(self, vin):
        vehicle = FakeVehicle(self._client, vin)
        self[vin] = vehicle
        return vehicle

    async def list(self):
        return {"response": self._client.vehicle_list}


class FakeTeslaClient:
    """Stands in for tesla_fleet_api.TeslaFleetOAuth."""

    def __init__(self, session, region, client_id, client_secret, refresh_token):
        self.session = session
        self.region = region
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.data = {
            "charge_state": charge_state(),
            "drive_state": drive_state(),
        }
        self.vehicle_list = [{"vin": "5YJSA1E2XKF000001"}]
        self.vehicle_data_errors: list[Exception] = []
        self._rotate_next = None
        self.vehicles = FakeVehicles(self)

    def rotate_refresh_token(self):
        if self._rotate_next:
            self.refresh_token = self._rotate_next
            self._rotate_next = None


def make_collector(tmp_path, client=None, **kwargs):
    holder = {}

    def factory(session, region, client_id, client_secret, refresh_token):
        c = client or FakeTeslaClient(session, region, client_id, client_secret, refresh_token)
        holder["client"] = c
        return c

    defaults = dict(
        client_id="cid",
        client_secret="csecret",
        refresh_token="initial-refresh",
        token_file=tmp_path / "tesla_token.json",
        vin="5YJSA1E2XKF000001",
        home_lat=HOME_LAT,
        home_lon=HOME_LON,
        client_factory=factory,
        session_factory=lambda: None,
    )
    defaults.update(kwargs)
    collector = TeslaCollector(**defaults)
    return collector, holder


def setup(collector):
    asyncio.run(collector.setup())
    return collector


class TestReadings:
    def test_awake_and_charging_yields_power_math(self, tmp_path, transactional_db):
        collector, _ = make_collector(tmp_path)
        setup(collector)
        readings = asyncio.run(collector.poll())
        assert [r.metric for r in readings] == ["ev_charge_power_w"]
        assert readings[0].value == pytest.approx(32.0 * 240.0)
        assert readings[0].unit == "W"
        assert readings[0].duration_s == 60

    def test_awake_and_not_charging_is_explicit_zero(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        holder["client"].data["charge_state"] = charge_state(charging=False)
        readings = asyncio.run(collector.poll())
        assert readings[0].value == 0.0  # a real zero, not an absence

    def test_charging_away_from_home_reads_zero(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        holder["client"].data["drive_state"] = drive_state(AWAY_LAT, AWAY_LON)
        readings = asyncio.run(collector.poll())
        assert readings[0].value == 0.0  # charging, but not home -- doesn't count

    def test_charging_with_unknown_location_still_records_full_value(
        self, tmp_path, transactional_db
    ):
        """Missing/unshared location must never silently drop real home
        charging -- home detection only suppresses a CONFIRMED away reading."""
        collector, holder = make_collector(tmp_path)
        setup(collector)
        holder["client"].data["drive_state"] = None
        readings = asyncio.run(collector.poll())
        assert readings[0].value == pytest.approx(32.0 * 240.0)


class TestAsleep:
    def test_vehicle_offline_is_a_failed_run_not_a_zero_sample(self, tmp_path, transactional_db):
        """tesla-fleet-api's TeslaFleetError subclasses BaseException, not
        Exception -- Collector.run_once only catches Exception, so this
        also proves poll() re-raises as an ordinary exception rather than
        letting a sleeping car's 408 escape run_once's failure handling."""
        collector, holder = make_collector(tmp_path)
        setup(collector)
        holder["client"].vehicle_data_errors = [VehicleOffline()]

        ok = asyncio.run(collector.run_once())

        assert ok is False
        assert Sample.objects.count() == 0
        assert CoverageSpan.objects.count() == 0
        run = CollectorRun.objects.get()
        assert run.ok is False


class TestStorage:
    def test_run_once_writes_sample_and_live_coverage(self, tmp_path, transactional_db):
        collector, _ = make_collector(tmp_path)
        setup(collector)
        ok = asyncio.run(collector.run_once())
        assert ok is True
        assert Sample.objects.count() == 1
        assert CoverageSpan.objects.get().state == CoverageSpan.State.LIVE
        assert Source.objects.get(slug="tesla").kind == Source.Kind.EV


class TestVinDiscovery:
    def test_blank_vin_discovers_accounts_first_vehicle(self, tmp_path, transactional_db):
        collector, _ = make_collector(tmp_path, vin=None)
        setup(collector)
        assert collector.vin == "5YJSA1E2XKF000001"


class TestTokenLifecycle:
    def test_rotated_token_is_persisted(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        holder["client"]._rotate_next = "rotated-refresh"
        asyncio.run(collector.poll())
        assert json.loads(collector.token_file.read_text()) == {
            "refresh_token": "rotated-refresh"
        }

    def test_cached_token_used_on_next_setup(self, tmp_path, transactional_db):
        token_file = tmp_path / "tesla_token.json"
        token_file.write_text(json.dumps({"refresh_token": "cached-refresh"}))
        collector, holder = make_collector(tmp_path, token_file=token_file)
        setup(collector)
        assert holder["client"].refresh_token == "cached-refresh"


class TestRegistry:
    def test_disabled_without_credentials(self, settings, caplog):
        settings.TESLA_CLIENT_ID = ""
        settings.TESLA_REFRESH_TOKEN = ""
        with caplog.at_level("WARNING"):
            collectors = enabled_collectors()
        assert not any(isinstance(c, TeslaCollector) for c in collectors)
        assert "tesla" in caplog.text

    def test_enabled_with_credentials(self, settings):
        settings.TESLA_CLIENT_ID = "cid"
        settings.TESLA_CLIENT_SECRET = "csecret"
        settings.TESLA_REFRESH_TOKEN = "rtok"
        settings.TESLA_VIN = "5YJSA1E2XKF000001"
        collectors = enabled_collectors()
        matches = [c for c in collectors if isinstance(c, TeslaCollector)]
        assert len(matches) == 1
        assert matches[0].client_id == "cid"
        assert matches[0].vin == "5YJSA1E2XKF000001"
