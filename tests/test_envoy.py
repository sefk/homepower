"""Envoy collector: reading conversion and the token lifecycle.

pyenphase's own parsing is its problem; these tests fake the Envoy at
the object level with real pyenphase dataclasses and exercise our side:
what becomes a Reading, and how the cached JWT is used, refreshed, and
persisted.
"""

import asyncio
import json

import pytest

from pyenphase import EnvoyAuthenticationError
from pyenphase.models.envoy import EnvoyData
from pyenphase.models.system_consumption import EnvoySystemConsumption
from pyenphase.models.system_production import EnvoySystemProduction

from collectors.envoy import EnvoyCollector


def envoy_data(production_w=1234.0, consumption_w=None):
    kwargs = dict.fromkeys(
        (f.name for f in EnvoyData.__dataclass_fields__.values()), None
    )
    kwargs["system_production"] = EnvoySystemProduction(
        watt_hours_lifetime=1, watt_hours_last_7_days=1, watt_hours_today=1,
        watts_now=production_w,
    )
    if consumption_w is not None:
        kwargs["system_consumption"] = EnvoySystemConsumption(
            watt_hours_lifetime=1, watt_hours_last_7_days=1, watt_hours_today=1,
            watts_now=consumption_w,
        )
    return EnvoyData(**kwargs)


class FakeAuth:
    def __init__(self, token):
        self.token = token


class FakeEnvoy:
    """Stands in for pyenphase.Envoy: records auth calls, serves canned data."""

    def __init__(self, host):
        self.host = host
        self.auth = None
        self.auth_calls = []
        self.data = envoy_data()
        self.valid_tokens = {"good-token"}
        self.update_errors = []

    async def setup(self):
        pass

    async def authenticate(self, username=None, password=None, token=None):
        self.auth_calls.append({"username": username, "token": token})
        if token is not None:
            if token not in self.valid_tokens:
                raise EnvoyAuthenticationError("token rejected")
            self.auth = FakeAuth(token)
        else:
            self.auth = FakeAuth("fresh-token")
            self.valid_tokens.add("fresh-token")

    async def update(self):
        if self.update_errors:
            raise self.update_errors.pop(0)
        return self.data


@pytest.fixture
def collector(tmp_path, transactional_db):
    c = EnvoyCollector(
        host="10.10.0.222",
        username="user@example.com",
        password="hunter2",
        token_file=tmp_path / "token.json",
        envoy_factory=FakeEnvoy,
    )
    yield c


def setup(collector):
    asyncio.run(collector.setup())
    return collector._envoy


class TestReadings:
    def test_production_only(self, collector):
        setup(collector)
        readings = asyncio.run(collector.poll())
        assert [r.metric for r in readings] == ["production_w"]
        assert readings[0].value == 1234.0
        assert readings[0].unit == "W"
        assert readings[0].duration_s == 60

    def test_consumption_when_ct_present(self, collector):
        envoy = setup(collector)
        envoy.data = envoy_data(production_w=800.0, consumption_w=450.0)
        readings = asyncio.run(collector.poll())
        assert {r.metric: r.value for r in readings} == {
            "production_w": 800.0,
            "consumption_w": 450.0,
        }


class TestTokenLifecycle:
    def test_first_auth_uses_cloud_and_saves_token(self, collector):
        setup(collector)
        assert (json.loads(collector.token_file.read_text())) == {"token": "fresh-token"}

    def test_cached_token_skips_cloud(self, collector):
        collector.token_file.write_text(json.dumps({"token": "good-token"}))
        envoy = setup(collector)
        assert envoy.auth_calls == [{"username": None, "token": "good-token"}]

    def test_stale_cached_token_falls_back_to_cloud(self, collector):
        collector.token_file.write_text(json.dumps({"token": "expired"}))
        envoy = setup(collector)
        assert envoy.auth_calls[0]["token"] == "expired"
        assert envoy.auth_calls[1]["username"] == "user@example.com"
        assert json.loads(collector.token_file.read_text())["token"] == "fresh-token"

    def test_token_rejected_mid_run_reauths_and_retries(self, collector):
        envoy = setup(collector)
        envoy.update_errors = [EnvoyAuthenticationError("jwt expired")]
        readings = asyncio.run(collector.poll())  # must not raise
        assert readings[0].metric == "production_w"
        assert envoy.auth_calls[-1]["username"] == "user@example.com"  # re-minted
