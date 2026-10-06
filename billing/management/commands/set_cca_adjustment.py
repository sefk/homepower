"""Enter a CCA adjustment (WestLight generation - PG&E generation credit +
PCIA, $/kWh) read off a bill. The project has no Django admin.

    manage.py set_cca_adjustment winter peak 2026-10-01 0.0816 --note "Nov 2026 bill"
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from billing.models import CcaAdjustment, Period, Season


class Command(BaseCommand):
    help = "Create or revise a CcaAdjustment row"

    def add_arguments(self, parser):
        parser.add_argument("season", choices=Season.values)
        parser.add_argument("period", choices=Period.values)
        parser.add_argument("effective_from", help="YYYY-MM-DD")
        parser.add_argument("amount", type=float, help="$/kWh; negative lowers the rate")
        parser.add_argument("--note", default="")

    def handle(self, *args, season, period, effective_from, amount, note, **options):
        try:
            eff = date.fromisoformat(effective_from)
        except ValueError:
            raise CommandError(f"not a date: {effective_from!r}")
        row, created = CcaAdjustment.objects.update_or_create(
            season=season, period=period, effective_from=eff,
            defaults={"amount": amount, "note": note},
        )
        self.stdout.write(f"{'created' if created else 'updated'}: {row}")
