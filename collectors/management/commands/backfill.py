"""Backfill sources from their vendor clouds' history.

    manage.py backfill                    # every configured source, all history
    manage.py backfill solaredge --start 2026-09-01 --end 2026-09-30

Safe to re-run: samples upsert, coverage merges, and intervals the
collectors saw live are left alone (see collectors.backfill). The grid
has no vendor cloud to ask — its history comes from PG&E Green Button
files via `manage.py import_greenbutton`.
"""

import asyncio
from datetime import date, timedelta

import aiohttp
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from collectors.backfill import BackfillResult, store_backfill
from collectors.enlighten import EnlightenHistory
from collectors.envoy import EnvoyCollector
from collectors.solaredge import SolarEdgeCollector
from collectors.solaredge_auth import SolarEdgeAuth

# The portal rejects quarter-hour ranges somewhere between 7 and 30 days.
SOLAREDGE_CHUNK_DAYS = 7
# Unpublished APIs: stay well under anything that looks like scraping.
PAUSE_S = 0.25


class Command(BaseCommand):
    help = "Backfill sources from vendor cloud history (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "sources",
            nargs="*",
            help=f"Sources to backfill (default: all of {', '.join(BACKFILLS)}).",
        )
        parser.add_argument(
            "--start",
            type=date.fromisoformat,
            help="First day, YYYY-MM-DD (default: the vendor's earliest).",
        )
        parser.add_argument(
            "--end", type=date.fromisoformat, help="Last day, YYYY-MM-DD (default: today)."
        )

    def handle(self, *args, **options):
        end = options["end"] or timezone.localdate()
        unknown = [s for s in options["sources"] if s not in BACKFILLS]
        if unknown:
            raise CommandError(
                f"no backfill for {', '.join(unknown)} (have: {', '.join(BACKFILLS)})"
            )
        for slug in options["sources"] or list(BACKFILLS):
            try:
                total = asyncio.run(BACKFILLS[slug](self, options["start"], end))
            except NotConfigured as exc:
                self.stdout.write(f"{slug}: skipped, {exc}")
                continue
            except (aiohttp.ClientError, RuntimeError) as exc:
                raise CommandError(f"{slug}: {exc}") from exc
            self.stdout.write(
                self.style.SUCCESS(
                    f"{slug}: {total.written} samples written, "
                    f"{total.skipped_live} skipped (already collected live)"
                )
            )

    def progress(self, slug, start, end, result):
        self.stdout.write(
            f"{slug}: {start} .. {end}  {result.written} written, {result.skipped_live} live"
        )


class NotConfigured(Exception):
    pass


def _add(total: BackfillResult, part: BackfillResult) -> None:
    total.written += part.written
    total.skipped_live += part.skipped_live


async def backfill_solaredge(cmd, start: date | None, end: date) -> BackfillResult:
    if not (
        settings.SOLAREDGE_USERNAME
        and settings.SOLAREDGE_PASSWORD
        and settings.SOLAREDGE_SITE_ID
    ):
        raise NotConfigured("no portal login/site id in .env")
    collector = SolarEdgeCollector(
        auth=SolarEdgeAuth(
            username=settings.SOLAREDGE_USERNAME,
            password=settings.SOLAREDGE_PASSWORD,
            token_file=settings.SOLAREDGE_TOKEN_FILE,
        ),
        site_id=settings.SOLAREDGE_SITE_ID,
    )
    source = await sync_to_async(collector._ensure_source)()
    total = BackfillResult()
    async with aiohttp.ClientSession() as session:
        cursor = start or await collector.installation_date(session)
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=SOLAREDGE_CHUNK_DAYS - 1), end)
            readings = await collector.fetch_power(session, cursor, chunk_end)
            result = await sync_to_async(store_backfill)(source, readings)
            cmd.progress("solaredge", cursor, chunk_end, result)
            _add(total, result)
            cursor = chunk_end + timedelta(days=1)
            await asyncio.sleep(PAUSE_S)
    return total


async def backfill_envoy(cmd, start: date | None, end: date) -> BackfillResult:
    if not (settings.ENPHASE_USERNAME and settings.ENPHASE_PASSWORD):
        raise NotConfigured("no Enlighten credentials in .env")
    collector = EnvoyCollector(
        host=settings.ENVOY_HOST,
        username=settings.ENPHASE_USERNAME,
        password=settings.ENPHASE_PASSWORD,
        token_file=settings.ENPHASE_TOKEN_FILE,
    )
    source = await sync_to_async(collector._ensure_source)()
    history = EnlightenHistory(settings.ENPHASE_USERNAME, settings.ENPHASE_PASSWORD)
    total = BackfillResult()
    tz = timezone.get_current_timezone()
    async with aiohttp.ClientSession() as session:
        await history.login(session)
        cursor = start or await history.first_day(session)
        while cursor <= end:
            readings, days = await history.fetch(session, cursor)
            if days == 0:
                break
            last = min(cursor + timedelta(days=days - 1), end)
            # The vendor picks the window length; honour --end ourselves.
            readings = [r for r in readings if r.ts.astimezone(tz).date() <= end]
            result = await sync_to_async(store_backfill)(source, readings)
            cmd.progress("envoy", cursor, last, result)
            _add(total, result)
            cursor += timedelta(days=days)
            await asyncio.sleep(PAUSE_S)
    return total


BACKFILLS = {"solaredge": backfill_solaredge, "envoy": backfill_envoy}
