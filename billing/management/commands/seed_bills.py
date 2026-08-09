"""Idempotent seed of the historical bill data (PRD build order: bill
seeding makes cost analyses real before any live grid data exists).
Re-running updates rows in place rather than duplicating them.
"""

from django.core.management.base import BaseCommand

from billing.data import ELECTRIC_BILLS, GAS_BILLS
from billing.models import BillPeriod, GasBillPeriod


class Command(BaseCommand):
    help = "Seed BillPeriod/GasBillPeriod from the transcribed historical bill data"

    def handle(self, *args, **options):
        created = updated = 0
        for end_date, peak_kwh, offpeak_kwh, net_kwh, nem_charges in ELECTRIC_BILLS:
            _, was_created = BillPeriod.objects.update_or_create(
                end_date=end_date,
                defaults={
                    "peak_kwh": peak_kwh,
                    "offpeak_kwh": offpeak_kwh,
                    "net_kwh": net_kwh,
                    "nem_charges": nem_charges,
                },
            )
            created += was_created
            updated += not was_created
        self.stdout.write(f"electric: {created} created, {updated} updated")

        created = updated = 0
        for start_date, end_date, therms, charges in GAS_BILLS:
            _, was_created = GasBillPeriod.objects.update_or_create(
                end_date=end_date,
                defaults={
                    "start_date": start_date,
                    "therms": therms,
                    "charges": charges,
                },
            )
            created += was_created
            updated += not was_created
        self.stdout.write(f"gas: {created} created, {updated} updated")
