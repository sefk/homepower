"""Mac Studio collector — this host's own wall-adjacent power draw.

macmon (`brew install macmon`) reads the SMC's system power sensor
without sudo; its `sys_power` is the whole machine's DC draw, so the
power supply's conversion loss (a few watts) isn't in it. One short
sample per minute, held for the minute like the Envoy's watts_now.

The collector runs on the machine it measures, so when studio sleeps or
the process is down there are no polls and the gap reads as unknown —
which is the truth.
"""

import asyncio
import json

from django.utils import timezone

from core.models import Source

from .base import Collector, Reading

# macmon averages its power readings over this window (ms).
SAMPLE_MS = 2000
TIMEOUT_S = 15


class MacmonCollector(Collector):
    slug = "studio"
    name = "Mac Studio"
    kind = Source.Kind.DEVICE
    poll_interval_s = 60
    native_resolution_s = 60

    def __init__(self, macmon_path: str):
        super().__init__()
        self.macmon_path = macmon_path

    async def poll(self) -> list[Reading]:
        ts = timezone.now().replace(microsecond=0)
        data = json.loads(await self._sample())
        return [
            Reading(
                metric="power_w",
                unit="W",
                ts=ts,
                duration_s=self.native_resolution_s,
                value=float(data["sys_power"]),
            )
        ]

    async def _sample(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.macmon_path, "pipe", "-s", "1", "-i", str(SAMPLE_MS),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"macmon timed out after {TIMEOUT_S}s")
        if proc.returncode != 0:
            raise RuntimeError(f"macmon exited {proc.returncode}: {err.decode().strip()}")
        return out.decode()
