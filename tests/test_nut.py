"""Network Closet collector: a NUT LIST VAR reply becomes a derived power_w
plus UPS stats, and a refusal or dead server becomes a failed poll."""

import asyncio

import pytest

from collectors.nut import NutCollector

LISTING = """BEGIN LIST VAR ups
VAR ups battery.charge "100"
VAR ups battery.runtime "2320"
VAR ups input.voltage "119.0"
VAR ups ups.load "21"
VAR ups ups.realpower.nominal "600"
VAR ups ups.status "OL"
END LIST VAR ups
"""


def serve(reply):
    """Run a one-shot fake NUT server; returns (server, port)."""

    async def handle(reader, writer):
        await reader.readline()
        writer.write(reply.encode())
        await writer.drain()
        writer.close()

    return handle


def poll_against(reply):
    async def run():
        server = await asyncio.start_server(serve(reply), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            return await NutCollector("127.0.0.1", port=port).poll()

    return asyncio.run(run())


def test_derives_watts_and_collects_extras():
    by_metric = {r.metric: r for r in poll_against(LISTING)}
    assert by_metric["power_w"].value == pytest.approx(126.0)
    assert by_metric["power_w"].unit == "W"
    assert by_metric["power_w"].duration_s == 60
    assert by_metric["ups_load_pct"].value == 21
    assert by_metric["battery_charge_pct"].value == 100
    assert by_metric["battery_runtime_s"].value == 2320
    assert by_metric["input_voltage_v"].value == pytest.approx(119.0)
    assert by_metric["on_line"].value == 1


def test_on_battery_reads_zero():
    listing = LISTING.replace('"OL"', '"OB DISCHRG"')
    by_metric = {r.metric: r for r in poll_against(listing)}
    assert by_metric["on_line"].value == 0


def test_only_power_is_a_watt_series():
    units = {r.metric: r.unit for r in poll_against(LISTING)}
    assert [m for m, u in units.items() if u == "W"] == ["power_w"]


def test_access_denied_raises():
    with pytest.raises(RuntimeError, match="ACCESS-DENIED"):
        poll_against("ERR ACCESS-DENIED\n")


def test_missing_load_raises():
    listing = LISTING.replace('VAR ups ups.load "21"\n', "")
    with pytest.raises(KeyError):
        poll_against(listing)


def test_connection_refused_raises():
    with pytest.raises(OSError):
        asyncio.run(NutCollector("127.0.0.1", port=1).poll())
