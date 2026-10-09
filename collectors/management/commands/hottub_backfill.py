"""Derive the Hot Tub (estimated) load from the Eagle's history.

    manage.py hottub_backfill [--since 2026-08-09] [--dry-run]

Same derivation as the 15-minute collector, over a longer window; safe to
rerun (each day is replaced, not appended to).
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from collectors import hottub_estimate


def _t(epoch, tz):
    return "--:--:--" if epoch is None else f"{timezone.localtime(hottub_estimate._utc(epoch), tz):%H:%M:%S}"


class Command(BaseCommand):
    help = "Backfill the Hot Tub (estimated) series from whole-home meter step changes"

    def add_arguments(self, parser):
        parser.add_argument("--since", type=date.fromisoformat, default=date(2026, 8, 9))
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, since, dry_run, **options):
        now = timezone.now()
        tz = timezone.get_current_timezone()
        today = timezone.localtime(now).date()
        results = hottub_estimate.derive_range(since, today, now, write=not dry_run)
        detected = misses = skipped = 0
        kwh = {}
        for r in results:
            for hour in r.skipped:
                skipped += 1
                self.stdout.write(f"{r.day} {hour:02d}h  no Eagle coverage -- left unknown")
            for w in r.windows:
                if w.heater_on is None:
                    misses += 1
                else:
                    detected += 1
                kwh[r.day] = kwh.get(r.day, 0) + w.kwh
                self.stdout.write(
                    f"{r.day} {w.hour:02d}h  on {_t(w.heater_on, tz)}  off {_t(w.heater_off, tz)}"
                    f"  {w.heater_minutes:5.1f} min  {w.heater_w or 0:5.0f} W"
                    f"  pump {w.pump_w:4.0f} W  {w.kwh:5.2f} kWh  {','.join(w.flags)}"
                )
        full_days = [d for d, r in ((r.day, r) for r in results) if r.windows and r.day < today]
        avg = sum(kwh[d] for d in full_days) / len(full_days) if full_days else 0.0
        self.stdout.write(
            f"{len(results)} days, {detected} heater runs detected, {misses} windows with no "
            f"heater detected, {skipped} windows unobserved; "
            f"avg {avg:.2f} kWh/day over {len(full_days)} complete days "
            f"({'dry run, nothing written' if dry_run else 'written'})"
        )
