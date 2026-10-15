"""Network Closet collector — the UPS behind the switch and the Synology.

The Synology reads the APC Back-UPS over USB and republishes it through
NUT (Network UPS Tools) on port 3493; DSM only answers hosts listed under
Control Panel → Hardware & Power → UPS → Permitted DiskStation Devices.
The UPS reports no real watts, so power_w is derived: ups.load is a whole
percent of ups.realpower.nominal, which makes it good to about 6 W on a
600 W unit.

The other readings (battery, input voltage, load percent) are collected
for a future UPS page; only power_w is a W series, so only it joins the
Sinks charts. A failed poll is a gap, never a zero.
"""

import asyncio

from django.utils import timezone

from core.models import Source

from .base import Collector, Reading

TIMEOUT_S = 10

# NUT variable -> (metric, unit)
EXTRAS = {
    "ups.load": ("ups_load_pct", "%"),
    "battery.charge": ("battery_charge_pct", "%"),
    "battery.runtime": ("battery_runtime_s", "s"),
    "input.voltage": ("input_voltage_v", "V"),
}


def parse_vars(listing: str, ups: str) -> dict[str, str]:
    """`VAR <ups> <name> "<value>"` lines of a LIST VAR reply, as a dict."""
    prefix = f"VAR {ups} "
    out = {}
    for line in listing.splitlines():
        if line.startswith(prefix):
            name, _, value = line[len(prefix):].partition(" ")
            out[name] = value.strip().strip('"')
    return out


class NutCollector(Collector):
    slug = "closet"
    name = "Network Closet"
    kind = Source.Kind.DEVICE
    poll_interval_s = 60
    native_resolution_s = 60

    def __init__(self, host: str, ups: str = "ups", port: int = 3493):
        super().__init__()
        self.host = host
        self.ups = ups
        self.port = port

    async def poll(self) -> list[Reading]:
        ts = timezone.now().replace(microsecond=0)
        data = parse_vars(await self._fetch(), self.ups)
        load = float(data["ups.load"])
        nominal = float(data["ups.realpower.nominal"])

        def reading(metric, unit, value):
            return Reading(
                metric=metric, unit=unit, ts=ts,
                duration_s=self.native_resolution_s, value=float(value),
            )

        readings = [reading("power_w", "W", load / 100 * nominal)]
        for var, (metric, unit) in EXTRAS.items():
            if var in data:
                readings.append(reading(metric, unit, data[var]))
        if "ups.status" in data:
            # OL = on line; OB (on battery) is the one worth knowing about.
            readings.append(
                reading("on_line", "bool", "OL" in data["ups.status"].split())
            )
        return readings

    async def _fetch(self) -> str:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), TIMEOUT_S
        )
        try:
            writer.write(f"LIST VAR {self.ups}\n".encode())
            await writer.drain()
            end = f"END LIST VAR {self.ups}"
            lines = []
            while True:
                raw = await asyncio.wait_for(reader.readline(), TIMEOUT_S)
                if not raw:
                    raise RuntimeError("NUT server closed the connection mid-reply")
                line = raw.decode().strip()
                if line.startswith("ERR "):
                    raise RuntimeError(f"NUT: {line}")
                if line == end:
                    return "\n".join(lines)
                lines.append(line)
        finally:
            writer.close()
