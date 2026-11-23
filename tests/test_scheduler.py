"""run_collector waits out a collector's startup delay before setup."""

import asyncio

import pytest

from collectors import scheduler


class Stop(BaseException):  # not Exception: setup failures are retried
    pass


class FakeCollector:
    slug = "fake"
    poll_interval_s = 60

    def __init__(self, delay, events):
        self.delay = delay
        self.events = events

    def startup_delay_s(self):
        return self.delay

    async def setup(self):
        self.events.append("setup")
        raise Stop  # end the loop once the ordering is known


def run(collector, events, monkeypatch):
    async def fake_sleep(seconds):
        events.append(("sleep", seconds))

    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)
    with pytest.raises(Stop):
        asyncio.run(scheduler.run_collector(collector))


def test_waits_before_setup(monkeypatch):
    events = []
    run(FakeCollector(1234.0, events), events, monkeypatch)
    assert events == [("sleep", 1234.0), "setup"]


def test_no_delay_sets_up_at_once(monkeypatch):
    events = []
    run(FakeCollector(0.0, events), events, monkeypatch)
    assert events == ["setup"]
