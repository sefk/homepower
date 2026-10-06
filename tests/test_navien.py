"""Navien collector: a status reply becomes one power_w reading, and a
unit that never answers is a failed poll, not a zero."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from collectors import navien
from collectors.navien import NavienCollector


class FakeMqtt:
    """Answers each status request with the next canned status, from
    another thread like the real AWS IoT client; None means no reply."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.callback = None
        self.requests = 0

    async def subscribe_device_status(self, device, callback):
        self.callback = callback

    async def request_device_status(self, device):
        self.requests += 1
        reply = self.replies.pop(0)
        if reply is not None:
            t = threading.Thread(target=self.callback, args=(reply,))
            t.start()
            t.join()


def make_collector(replies):
    mqtt = FakeMqtt(replies)

    async def connect(username, password):
        return object(), SimpleNamespace(name="NWP500"), mqtt

    return NavienCollector("u@example.com", "pw", connect=connect), mqtt


@pytest.fixture
def setup(transactional_db):
    def run(collector):
        asyncio.run(collector.setup())
        return collector
    return run


def test_setup_subscribes(setup):
    collector, mqtt = make_collector([])
    setup(collector)
    assert mqtt.callback == collector._on_status


def test_status_becomes_power_reading(setup):
    collector, mqtt = make_collector([SimpleNamespace(current_inst_power=5480)])
    [reading] = asyncio.run(setup(collector).poll())
    assert (reading.metric, reading.unit, reading.value) == ("power_w", "W", 5480.0)
    assert reading.duration_s == 60
    assert mqtt.requests == 1


def test_each_poll_waits_for_its_own_reply(setup):
    collector, _ = make_collector(
        [SimpleNamespace(current_inst_power=450), SimpleNamespace(current_inst_power=0)]
    )
    setup(collector)
    assert [asyncio.run(collector.poll())[0].value for _ in range(2)] == [450.0, 0.0]


def test_no_reply_fails_the_poll(setup, monkeypatch):
    monkeypatch.setattr(navien, "STATUS_TIMEOUT_S", 0.05)
    collector, _ = make_collector([None])
    setup(collector)
    with pytest.raises(RuntimeError, match="no status reply"):
        asyncio.run(collector.poll())
    # A late reply after the timeout lands nowhere instead of raising.
    collector._on_status(SimpleNamespace(current_inst_power=1))
