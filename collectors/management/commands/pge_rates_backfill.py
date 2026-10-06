"""Recover PG&E rate-change dates from Opower's hourly cost history.

    manage.py pge_rates_backfill --days 400 [--dry-run]

Same derivation as the daily pge_rates collector, over a longer window.
--dry-run only reads (PG&E and the rate table) and prints the rate eras
Opower shows plus the UtilityRate changes that would result.
"""

import asyncio
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from collectors import pge_rates


class Command(BaseCommand):
    help = "Backfill UtilityRate rows from PG&E's hourly cost history (via Opower)"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, days, dry_run, **options):
        if not settings.PGE_USERNAME or not settings.PGE_PASSWORD:
            raise CommandError("set PGE_USERNAME and PGE_PASSWORD in .env first")
        end = timezone.now()
        start = end - timedelta(days=days)
        try:
            reads = asyncio.run(
                pge_rates.fetch_cost_reads(
                    settings.PGE_USERNAME, settings.PGE_PASSWORD, settings.PGE_LOGIN_FILE, start, end
                )
            )
        except RuntimeError as exc:
            raise CommandError(str(exc))
        self.report(reads, dry_run)

    def report(self, reads, dry_run):
        daily = pge_rates.derive_daily_rates(reads)
        self.stdout.write(f"{len(reads)} hourly reads over {len(daily)} days")
        if daily:
            self.stdout.write(f"  {min(daily)} .. {max(daily)}")
        self.stdout.write(f"Rate eras (days before {pge_rates.billing_rates.OPOWER_BASIS_START} ignored):")
        for key, eras in sorted(pge_rates.rate_eras(daily).items()):
            for first, last, rate in eras:
                self.stdout.write(f"  {key[0]:6} {key[1]:7} tier {key[2]}  {first} .. {last}  {rate:.4f}")
        changes = pge_rates.plan_changes(daily, pge_rates.existing_rows())
        self.stdout.write(f"{len(changes)} change(s) to the rate table:")
        for c in changes:
            self.stdout.write(f"  {pge_rates.describe(c)}{' (revises row)' if c.update else ''}")
        if not dry_run:
            pge_rates.apply_changes(changes)
            self.stdout.write("written")
