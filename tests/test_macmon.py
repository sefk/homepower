"""Mac Studio collector: macmon's JSON becomes one power_w reading, and
a macmon failure becomes a failed poll rather than a bogus zero."""

import asyncio
import json
import stat

import pytest

from collectors.macmon import MacmonCollector


def fake_macmon(tmp_path, body):
    script = tmp_path / "macmon"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_reads_sys_power(tmp_path):
    payload = json.dumps({"sys_power": 17.96, "cpu_power": 3.1})
    collector = MacmonCollector(fake_macmon(tmp_path, f"echo '{payload}'"))
    [reading] = asyncio.run(collector.poll())
    assert reading.metric == "power_w"
    assert reading.unit == "W"
    assert reading.value == pytest.approx(17.96)
    assert reading.duration_s == 60


def test_macmon_failure_raises(tmp_path):
    collector = MacmonCollector(fake_macmon(tmp_path, "echo boom >&2; exit 3"))
    with pytest.raises(RuntimeError, match="exited 3: boom"):
        asyncio.run(collector.poll())


def test_missing_sys_power_raises(tmp_path):
    collector = MacmonCollector(fake_macmon(tmp_path, "echo '{}'"))
    with pytest.raises(KeyError):
        asyncio.run(collector.poll())
