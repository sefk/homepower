"""Tesla Fleet API collector: reading conversion, home-charging geofence,
budgeted adaptive polling, charge-energy deltas, refresh-token
persistence, and registry gating.

Fakes the tesla-fleet-api client at the object level (session/region/
client_id/client_secret/refresh_token in, products()/
vehicles.createFleet(vin).vehicle()/.vehicle_data() out) so these tests exercise
our side only -- what becomes a Reading, and how a real
BaseException-subclassing vendor error (tesla-fleet-api's own choice,
not this codebase's) still surfaces as an ordinary failed run.
"""

import asyncio
import json
from datetime import timedelta

import pytest
from tesla_fleet_api.exceptions import VehicleOffline

from collectors.registry import enabled_collectors
from collectors.tesla import CHARGING_INTERVAL_S, IDLE_INTERVAL_S, TeslaCollector
from core.models import CollectorRun, CoverageSpan, Sample, Source

HOME_LAT, HOME_LON = 37.4419, -122.1430
# ~1.2km from HOME_LAT/HOME_LON -- well outside the 200m geofence.
AWAY_LAT, AWAY_LON = 37.4530, -122.1430


def charge_state(charging=True, current=32.0, voltage=240.0, added=None, state=None,
                 fast=False):
    return {
        "charging_state": state or ("Charging" if charging else "Disconnected"),
        "charger_actual_current": current,
        "charger_voltage": voltage,
        "charge_energy_added": added,
        "fast_charger_present": fast,
    }


def drive_state(lat=HOME_LAT, lon=HOME_LON):
    return {"latitude": lat, "longitude": lon}


class FakeVehicle:
    def __init__(self, client, vin):
        self._client = client
        self.vin = vin

    async def vehicle(self):
        self._client.calls.append("vehicle")
        return {"response": {"vin": self.vin, "state": self._client.state}}

    async def vehicle_data(self, endpoints=None):
        self._client.calls.append("vehicle_data")
        self._client.endpoints = endpoints
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
        # Products: vehicles plus any energy sites, which have no VIN.
        self.vehicle_list = [{"energy_site_id": 1}, {"vin": "5YJSA1E2XKF000001"}]
        self.state = "online"
        self.calls: list[str] = []
        self.endpoints = None
        self.vehicle_data_errors: list[Exception] = []
        self._rotate_next = None
        self.vehicles = FakeVehicles(self)

    async def products(self):
        self.calls.append("list")
        return {"response": self.vehicle_list}

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
        usage_file=tmp_path / "tesla_usage.json",
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
        assert readings[0].duration_s == CHARGING_INTERVAL_S

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


class TestAdaptivePolling:
    def test_asleep_is_a_known_zero_without_reading_vehicle_data(
        self, tmp_path, transactional_db
    ):
        """A charging car never sleeps, so asleep is a real 0 W -- learned
        from the state check alone, which doesn't wake the car."""
        collector, holder = make_collector(tmp_path)
        setup(collector)
        holder["client"].state = "asleep"
        readings = asyncio.run(collector.poll())
        assert holder["client"].calls == ["vehicle"]
        assert [(r.metric, r.value) for r in readings] == [("ev_charge_power_w", 0.0)]
        assert readings[0].duration_s == IDLE_INTERVAL_S
        assert collector.next_interval_s() == IDLE_INTERVAL_S

    def test_offline_is_unknown(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        holder["client"].state = "offline"
        assert asyncio.run(collector.run_once()) is False
        assert Sample.objects.count() == 0

    def test_charging_polls_fast_and_skips_the_state_check(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        asyncio.run(collector.poll())
        assert holder["client"].calls == ["vehicle", "vehicle_data"]
        assert collector.next_interval_s() == CHARGING_INTERVAL_S

        asyncio.run(collector.poll())
        assert holder["client"].calls == ["vehicle", "vehicle_data", "vehicle_data"]

    def test_charging_ends_back_to_idle_cadence(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        asyncio.run(collector.poll())
        holder["client"].data["charge_state"] = charge_state(state="Complete")
        readings = asyncio.run(collector.poll())
        assert readings[0].value == 0.0
        assert collector.next_interval_s() == IDLE_INTERVAL_S

    def test_car_sleeping_mid_session_falls_back_to_state_checks(
        self, tmp_path, transactional_db
    ):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        asyncio.run(collector.poll())
        holder["client"].vehicle_data_errors = [VehicleOffline()]
        assert asyncio.run(collector.run_once()) is False
        assert collector.next_interval_s() == IDLE_INTERVAL_S

    def test_supercharger_is_not_home_charging(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path, home_lat=None, home_lon=None)
        setup(collector)
        holder["client"].data["charge_state"] = charge_state(fast=True)
        readings = asyncio.run(collector.poll())
        assert readings[0].value == 0.0

    def test_location_requested_only_with_a_geofence(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path, home_lat=None, home_lon=None)
        setup(collector)
        asyncio.run(collector.poll())
        assert holder["client"].endpoints == ["charge_state"]

        collector, holder = make_collector(tmp_path)
        setup(collector)
        asyncio.run(collector.poll())
        assert holder["client"].endpoints == ["charge_state", "location_data"]


def energy_after(collector, holder, *added_states):
    """Poll once per (kwh, charging_state); return the energy readings."""
    out = []
    for kwh, state in added_states:
        holder["client"].data["charge_state"] = charge_state(added=kwh, state=state)
        out += [r for r in asyncio.run(collector.poll()) if r.metric == "ev_charge_energy_wh"]
    return out


class TestChargeEnergy:
    def test_deltas_of_the_session_counter_tile_the_reads(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        readings = energy_after(
            collector, holder, (1.0, "Charging"), (2.5, "Charging"), (4.0, "Complete")
        )
        # First read only sets the baseline: what came before is unknown.
        assert [r.value for r in readings] == [pytest.approx(1500.0), pytest.approx(1500.0)]
        assert readings[0].ts + timedelta(seconds=readings[0].duration_s) == readings[1].ts

    def test_new_session_after_unplug_counts_from_zero(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        readings = energy_after(
            collector,
            holder,
            (10.0, "Complete"),
            (10.0, "Disconnected"),
            (10.0, "Stopped"),  # plugged back in, counter not yet restarted
            (3.0, "Charging"),
        )
        assert [r.value for r in readings] == [0.0, 0.0, pytest.approx(3000.0)]

    def test_counter_reset_without_seeing_the_unplug(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        readings = energy_after(collector, holder, (10.0, "Complete"), (2.0, "Charging"))
        assert [r.value for r in readings] == [pytest.approx(2000.0)]

    def test_supercharger_energy_is_not_home_energy(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path)
        setup(collector)
        holder["client"].data["charge_state"] = charge_state(added=1.0, fast=True)
        asyncio.run(collector.poll())
        holder["client"].data["charge_state"] = charge_state(added=20.0, fast=True)
        readings = asyncio.run(collector.poll())
        assert readings[1].metric == "ev_charge_energy_wh"
        assert readings[1].value == 0.0


class TestRequestBudget:
    def test_spent_budget_stops_api_calls(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path, monthly_budget=3)
        setup(collector)
        asyncio.run(collector.poll())  # state check + vehicle_data = 2
        asyncio.run(collector.poll())  # charging: vehicle_data = 3
        assert asyncio.run(collector.run_once()) is False
        assert len(holder["client"].calls) == 3
        assert "budget" in CollectorRun.objects.get().message

    def test_count_survives_restart_and_resets_next_month(self, tmp_path, transactional_db):
        usage = tmp_path / "tesla_usage.json"
        usage.write_text(json.dumps({"month": "2020-01", "requests": 4000}))
        collector, holder = make_collector(tmp_path, monthly_budget=4000)
        setup(collector)
        asyncio.run(collector.poll())  # last month's spend doesn't count
        assert json.loads(usage.read_text())["requests"] == 2

        collector, holder = make_collector(tmp_path, monthly_budget=2)
        setup(collector)
        assert asyncio.run(collector.run_once()) is False
        assert holder["client"].calls == []

    def test_vin_discovery_is_billed(self, tmp_path, transactional_db):
        collector, holder = make_collector(tmp_path, vin=None)
        setup(collector)
        assert json.loads((tmp_path / "tesla_usage.json").read_text())["requests"] == 1


def test_fakes_only_use_methods_the_real_library_has():
    """The fakes once offered vehicles.list(), which tesla-fleet-api 1.8
    doesn't have: tests passed while the real collector failed setup.
    Every method a fake offers must exist on the class it stands in for."""
    from tesla_fleet_api import TeslaFleetOAuth
    from tesla_fleet_api.tesla.vehicle.fleet import VehicleFleet
    from tesla_fleet_api.tesla.vehicle.vehicles import Vehicles

    for fake, real in (
        (FakeTeslaClient, TeslaFleetOAuth),
        (FakeVehicles, Vehicles),
        (FakeVehicle, VehicleFleet),
    ):
        for name in vars(fake):
            if not name.startswith("_") and callable(getattr(fake, name)) and name != "rotate_refresh_token":
                assert hasattr(real, name), f"{fake.__name__}.{name} not on {real.__name__}"

