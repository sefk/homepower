"""Eagle 3 push ingest: RFA parsing, endpoint behavior, coverage semantics."""

from datetime import timedelta

import pytest

from collectors.eagle import collector, parse_rfa
from core.models import CollectorRun, CoverageSpan, Sample, Series, Source

from .conftest import utc

# 0x2ff09e00 seconds after 2000-01-01 = 2025-06-27 00:10:40 UTC; the exact
# instant matters less than that parsing round-trips it consistently.
DEMAND_XML = b"""<rainforest macId="0xd8d5b90000fda8" timestamp="0x2ff09e00s">
<InstantaneousDemand>
  <DeviceMacId>0xd8d5b90000fda8</DeviceMacId>
  <MeterMacId>0x00135003007c7d05</MeterMacId>
  <TimeStamp>0x2ff09e00</TimeStamp>
  <Demand>0x0005dc</Demand>
  <Multiplier>0x000001</Multiplier>
  <Divisor>0x0003e8</Divisor>
  <DigitsRight>0x03</DigitsRight>
  <DigitsLeft>0x0f</DigitsLeft>
  <SuppressLeadingZero>Y</SuppressLeadingZero>
</InstantaneousDemand>
</rainforest>"""

EXPORT_XML = b"""<rainforest macId="0xd8d5b90000fda8" timestamp="0x2ff09e0fs">
<InstantaneousDemand>
  <TimeStamp>0x2ff09e0f</TimeStamp>
  <Demand>0xfffffc18</Demand>
  <Multiplier>0x000001</Multiplier>
  <Divisor>0x0003e8</Divisor>
</InstantaneousDemand>
</rainforest>"""

SUMMATION_XML = b"""<rainforest macId="0xd8d5b90000fda8" timestamp="0x2ff09e00s">
<CurrentSummationDelivered>
  <TimeStamp>0x2ff09e00</TimeStamp>
  <SummationDelivered>0x0000000000a03b2f</SummationDelivered>
  <SummationReceived>0x00000000006b12c0</SummationReceived>
  <Multiplier>0x000001</Multiplier>
  <Divisor>0x0003e8</Divisor>
</CurrentSummationDelivered>
</rainforest>"""

DEMAND_JSON = b"""{"InstantaneousDemand": {
  "TimeStamp": "0x2ff09e00",
  "Demand": "0x0005dc",
  "Multiplier": "0x000001",
  "Divisor": "0x0003e8"
}}"""


@pytest.fixture(autouse=True)
def fresh_collector_state():
    """The module-level collector carries state; isolate tests from each other."""
    collector._source = None
    collector._last_ts = {}
    yield


class TestParser:
    def test_demand_scaled_kw_to_w(self):
        readings = parse_rfa(DEMAND_XML)
        assert len(readings) == 1
        r = readings[0]
        assert r.metric == "demand_w"
        assert r.value == 1500.0  # 0x5dc=1500, x1/1000 = 1.5 kW -> W
        assert r.ts == utc(2025, 6, 27, 0, 10, 40)

    def test_negative_demand_is_export(self):
        readings = parse_rfa(EXPORT_XML)
        assert readings[0].value == -1000.0  # 0xfffffc18 = -1000 two's complement

    def test_summation_counters(self):
        readings = parse_rfa(SUMMATION_XML)
        values = {r.metric: r.value for r in readings}
        assert values == {
            "energy_delivered_wh": 10500.911 * 1000,  # 0xa03b2f/1000 kWh -> Wh
            "energy_received_wh": 7017.152 * 1000,
        }
        assert all(r.unit == "Wh" for r in readings)

    def test_json_equivalent(self):
        readings = parse_rfa(DEMAND_JSON)
        assert readings[0].metric == "demand_w"
        assert readings[0].value == 1500.0

    def test_no_reading_sentinel_becomes_absence_not_sample(self):
        """0x800000 (sign-extended) means 'meter has no reading' — it must
        vanish into unknown coverage, never become a -8.4 MW sample."""
        for sentinel in (b"0xff800000", b"0x800000"):
            payload = DEMAND_XML.replace(b"0x0005dc", sentinel)
            assert parse_rfa(payload) == []

    def test_malformed_block_skipped_not_fatal(self):
        bad = DEMAND_XML.replace(b"0x0005dc", b"not-hex")
        assert parse_rfa(bad) == []

    def test_garbage_raises(self):
        with pytest.raises(Exception):
            parse_rfa(b"\x00\x01 not xml or json")


class TestEndpoint:
    def test_push_stores_samples_and_coverage(self, transactional_db, client):
        resp = client.post("/ingest/eagle/", DEMAND_XML, content_type="text/xml")
        assert resp.status_code == 200
        sample = Sample.objects.get()
        assert sample.series.metric == "demand_w"
        assert sample.value == 1500.0
        assert Source.objects.get(slug="eagle").kind == Source.Kind.GRID
        assert CoverageSpan.objects.count() == 1
        assert CollectorRun.objects.get().ok is True

    def test_consecutive_pushes_bridge_coverage(self, transactional_db, client):
        client.post("/ingest/eagle/", DEMAND_XML, content_type="text/xml")
        client.post("/ingest/eagle/", EXPORT_XML, content_type="text/xml")  # +15s
        span = CoverageSpan.objects.get()  # one merged span, not two slivers
        assert span.start == utc(2025, 6, 27, 0, 10, 40)

    def test_demand_gap_beyond_grace_stays_unknown(self, transactional_db, client):
        client.post("/ingest/eagle/", DEMAND_XML, content_type="text/xml")
        late = DEMAND_XML.replace(b"0x2ff09e00", b"0x2ff0a000")  # +8m32s
        client.post("/ingest/eagle/", late, content_type="text/xml")
        demand_spans = CoverageSpan.objects.filter(series__metric="demand_w")
        assert demand_spans.count() == 2  # outage between pushes stays unknown

    def test_counter_bridges_any_gap(self, transactional_db, client):
        client.post("/ingest/eagle/", SUMMATION_XML, content_type="text/xml")
        late = SUMMATION_XML.replace(b"0x2ff09e00", b"0x2ff0a000")
        client.post("/ingest/eagle/", late, content_type="text/xml")
        span = CoverageSpan.objects.filter(series__metric="energy_delivered_wh").get()
        assert span.start == utc(2025, 6, 27, 0, 10, 40)  # delta is exact knowledge

    def test_garbage_records_failed_run_and_saves_body(
        self, transactional_db, client, settings, tmp_path
    ):
        settings.VAR_DIR = tmp_path  # keep test artifacts out of the real var/
        resp = client.post(
            "/ingest/eagle/", b"\x00garbage", content_type="application/octet-stream"
        )
        assert resp.status_code == 200  # never make the firmware retry-loop
        run = CollectorRun.objects.get()
        assert run.ok is False
        saved = list((settings.VAR_DIR / "ingest-unparsed").glob("eagle-*.body"))
        assert len(saved) >= 1
        assert Sample.objects.count() == 0

    def test_get_is_405(self, db, client):
        assert client.get("/ingest/eagle/").status_code == 405

    def test_repush_same_payload_idempotent(self, transactional_db, client):
        client.post("/ingest/eagle/", DEMAND_XML, content_type="text/xml")
        client.post("/ingest/eagle/", DEMAND_XML, content_type="text/xml")
        assert Sample.objects.count() == 1
