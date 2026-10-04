"""PG&E natural gas, read from PG&E's Opower usage data.

There is no local gas meter to talk to: PG&E's smart meter reports once a
day, and the only way to its readings without a hand download is the
pge.com login the website uses, driven here through the `opower` library
(the same one Home Assistant's PG&E integration uses).

PG&E asks for a texted or emailed code the first time a device signs in.
`manage.py pge_auth` takes that code once and saves the remembered-device
cookie to settings.PGE_LOGIN_FILE; every poll signs in with it. When PG&E
forgets the device, polls fail with a message saying to rerun pge_auth —
a visible failed run, not a crash.

Readings are vendor history, not live observation, so they're stored the
way the Green Button importer stores them (core.history.replace_interval,
BACKFILLED coverage) — the two paths write the same series and must agree.
Each poll re-reads a trailing window, which picks up days PG&E posts late
and any it later corrects.
"""

import json
import logging
from datetime import timedelta
from pathlib import Path

import aiohttp
from django.db import transaction
from django.utils import timezone
from opower import AggregateType, MeterType, MfaChallenge, Opower

from core.history import replace_interval
from core.models import Series, Source

from .base import Collector, Reading

logger = logging.getLogger(__name__)

UTILITY = "pge"
# PG&E bills the US therm: 100,000 BTU.
WH_PER_THERM = 29_307.1
# Days re-read on every poll. PG&E posts a day 1-2 days late and
# occasionally revises; a month covers both with room to spare.
LOOKBACK_DAYS = 30
REAUTH = "PG&E wants a new sign-in code: run `manage.py pge_auth`"


def load_login_data(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_login_data(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


class PgeGasCollector(Collector):
    slug = "pge_gas"
    name = "PG&E Gas"
    kind = Source.Kind.GAS
    # PG&E's own cadence: one reading per day. Staleness on /health/ is
    # measured in these, so a day or two of PG&E lag doesn't read stale.
    poll_interval_s = 86400
    native_resolution_s = 86400
    # Checking more often than daily costs one login and catches a late
    # posting sooner.
    check_every_s = 6 * 3600

    def __init__(self, username: str, password: str, login_file: Path):
        super().__init__()
        self.username = username
        self.password = password
        self.login_file = login_file

    def next_interval_s(self) -> int:
        return self.check_every_s

    async def poll(self) -> list[Reading]:
        login_data = load_login_data(self.login_file)
        end = timezone.now()
        start = end - timedelta(days=LOOKBACK_DAYS)
        async with aiohttp.ClientSession() as session:
            opower = Opower(session, UTILITY, self.username, self.password, login_data=login_data)
            try:
                await opower.async_login()
            except MfaChallenge:
                raise RuntimeError(REAUTH) from None
            accounts = [a for a in await opower.async_get_accounts() if a.meter_type == MeterType.GAS]
            if not accounts:
                raise RuntimeError("no gas account on this PG&E login")
            readings = []
            for account in accounts:
                # Usage reads, not cost reads: async_get_cost_reads drops
                # trailing all-zero days as "not posted yet", which erases
                # real zero-gas days (summer, after the heat pump water
                # heater). The usage endpoint returns only posted days.
                reads = await opower.async_get_usage_reads(account, AggregateType.DAY, start, end)
                readings += [
                    Reading(
                        metric="gas_wh",
                        unit="Wh",
                        ts=r.start_time,
                        duration_s=int((r.end_time - r.start_time).total_seconds()),
                        value=r.consumption * WH_PER_THERM,
                    )
                    for r in reads
                ]
        return readings

    def _store(self, readings: list[Reading]) -> None:
        # Vendor history, so BACKFILLED via the importer's write path
        # rather than the base class's LIVE coverage bridging.
        with transaction.atomic():
            for r in readings:
                series, _ = Series.objects.get_or_create(
                    source=self._source, metric=r.metric, defaults={"unit": r.unit}
                )
                replace_interval(series, r.ts, r.duration_s, r.value)


def ensure_source() -> Source:
    """Get-or-create the pge_gas Source row with the collector's defaults.

    For the Green Button importer, which writes gas history to this
    Source without running the collector.
    """
    return PgeGasCollector("", "", Path())._ensure_source()
