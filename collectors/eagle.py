"""Rainforest Eagle 3 — whole-home grid telemetry, push not pull.

The Eagle's uploader POSTs RFA-format fragments to /ingest/eagle/ every
~10-30s: hex values scaled by Multiplier/Divisor, timestamps in seconds
since 2000-01-01 UTC, demand signed two's-complement (negative =
exporting to the grid). The meter's cumulative registers are stored raw
as counters — deltas are an analysis concern, and the counters make
energy across collector gaps recoverable later.

First contact is defensive: an unparseable body is saved to
var/ingest-unparsed/ (bounded) and recorded as a failed run, so a
firmware format surprise is diagnosable instead of a silent 500 loop.
"""

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.models import Source

from .base import Collector, Reading

logger = logging.getLogger(__name__)

Y2K = datetime(2000, 1, 1, tzinfo=dt_timezone.utc)
UNPARSED_KEEP = 20
# ZigBee SE metering "reading unavailable" sentinel: 0x800000 as signed
# 24-bit (arrives sign-extended to 0xff800000). Not a measurement — the
# meter saying "unknown", which for us means no sample at all.
DEMAND_SENTINEL = 0x800000


def _hex_int(text: str, bits: int | None = None) -> int:
    """Parse Eagle hex ('0x1a2b'); optionally as signed two's complement."""
    value = int(text, 16)
    if bits and value >= 1 << (bits - 1):
        value -= 1 << bits
    return value


def _block_ts(block: dict) -> datetime:
    return Y2K + timedelta(seconds=_hex_int(block["TimeStamp"]))


def _scaled(block: dict, field: str, signed: bool) -> float:
    raw = _hex_int(block[field], bits=32 if signed else None)
    multiplier = _hex_int(block.get("Multiplier", "0x1")) or 1
    divisor = _hex_int(block.get("Divisor", "0x1")) or 1
    return raw * multiplier / divisor


def parse_rfa(body: bytes) -> list[Reading]:
    """RFA uploader payload (XML or its JSON equivalent) -> Readings.

    Values on the wire are kW / kWh; stored as W / Wh.
    """
    text = body.decode("utf-8", errors="replace").strip()
    blocks: list[tuple[str, dict]] = []
    if text.startswith("{") or text.startswith("["):
        doc = json.loads(text)
        for item in doc if isinstance(doc, list) else [doc]:
            for name, block in item.items():
                if isinstance(block, dict):
                    blocks.append((name, block))
    else:
        root = ET.fromstring(text)
        elements = [root] if root.tag != "rainforest" else list(root)
        for el in elements:
            blocks.append((el.tag, {child.tag: (child.text or "") for child in el}))

    duration = settings.EAGLE_NOMINAL_INTERVAL_S
    readings = []
    for name, block in blocks:
        try:
            if name == "InstantaneousDemand":
                if abs(_hex_int(block["Demand"], bits=32)) == DEMAND_SENTINEL:
                    logger.debug("eagle: demand sentinel (no reading), skipping")
                    continue
                readings.append(
                    Reading(
                        metric="demand_w",
                        unit="W",
                        ts=_block_ts(block),
                        duration_s=duration,
                        value=round(_scaled(block, "Demand", signed=True) * 1000, 1),
                    )
                )
            elif name == "CurrentSummationDelivered":
                ts = _block_ts(block)
                for field, metric in (
                    ("SummationDelivered", "energy_delivered_wh"),
                    ("SummationReceived", "energy_received_wh"),
                ):
                    if field in block:
                        readings.append(
                            Reading(
                                metric=metric,
                                unit="Wh",
                                ts=ts,
                                duration_s=duration,
                                value=round(_scaled(block, field, signed=False) * 1000, 1),
                            )
                        )
        except (KeyError, ValueError) as exc:
            # one malformed block shouldn't sink its siblings
            logger.warning("eagle: skipping %s block: %r", name, exc)
    return readings


class EagleCollector(Collector):
    """Push-driven: the view stores readings; there is no poll loop."""

    slug = "eagle"
    name = "Grid (PG&E meter via Eagle 3)"
    kind = Source.Kind.GRID
    # cumulative registers: the delta is exact across any gap
    always_bridge = frozenset({"energy_delivered_wh", "energy_received_wh"})

    @property
    def grace_s(self) -> int:
        # Push cadence is variable (~8-30s observed/documented), so
        # "exactly 2x nominal" doesn't mean a push was missed the way it
        # does for a fixed-cadence poller. 4x nominal (60s) separates
        # normal cadence from real silence.
        return 4 * self.poll_interval_s

    def __init__(self):
        # nominal cadence drives staleness (3x) and coverage grace (2x)
        self.poll_interval_s = settings.EAGLE_NOMINAL_INTERVAL_S
        self.native_resolution_s = settings.EAGLE_NOMINAL_INTERVAL_S
        super().__init__()

    async def poll(self) -> list[Reading]:  # pragma: no cover
        raise NotImplementedError("eagle is push-driven")

    def store_push(self, readings: list[Reading], started: datetime) -> None:
        if self._source is None:
            self._source = self._ensure_source()
        self._store(readings)
        self._record_run(started, ok=True, message=f"{len(readings)} readings")

    def record_bad_push(self, started: datetime, message: str) -> None:
        if self._source is None:
            self._source = self._ensure_source()
        self._record_run(started, ok=False, message=message)


# Single process (manage.py serve), so one module-level instance carries
# the in-memory coverage bridge across pushes, same as a polling collector.
collector = EagleCollector()


def _save_unparsed(body: bytes) -> str:
    unparsed_dir = settings.VAR_DIR / "ingest-unparsed"
    unparsed_dir.mkdir(parents=True, exist_ok=True)
    stamp = timezone.now().strftime("%Y%m%dT%H%M%S.%f")
    path = unparsed_dir / f"eagle-{stamp}.body"
    path.write_bytes(body)
    for stale in sorted(unparsed_dir.glob("eagle-*.body"))[:-UNPARSED_KEEP]:
        stale.unlink()
    return str(path)


@csrf_exempt
def ingest(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    started = timezone.now()
    body = request.body
    try:
        readings = parse_rfa(body)
    except Exception as exc:
        path = _save_unparsed(body)
        logger.warning("eagle: unparseable push (%r), body saved to %s", exc, path)
        collector.record_bad_push(started, f"unparseable push: {exc!r}; saved {path}")
        # 200 on purpose: the Eagle can't fix the payload, and a non-2xx
        # may make some firmwares retry the same body forever
        return HttpResponse("unparsed")
    if readings:
        collector.store_push(readings, started)
    return HttpResponse("ok")
