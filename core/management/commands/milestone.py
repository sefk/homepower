"""Add, list or remove the milestones Grafana marks on its time charts.

    manage.py milestone list
    manage.py milestone add 2027-03-01 "Battery installed"
    manage.py milestone remove 4
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from core.models import Milestone


class Command(BaseCommand):
    help = "Manage the milestones marked on the Grafana time charts"

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("list")
        add = sub.add_parser("add")
        add.add_argument("date", help="local calendar day, YYYY-MM-DD")
        add.add_argument("label")
        remove = sub.add_parser("remove")
        remove.add_argument("id", type=int)

    def handle(self, *args, action, **options):
        if action == "add":
            try:
                day = date.fromisoformat(options["date"])
            except ValueError:
                raise CommandError(f"not a YYYY-MM-DD date: {options['date']!r}")
            m = Milestone.objects.create(date=day, label=options["label"])
            self.stdout.write(f"added {m.id}: {m}")
        elif action == "remove":
            deleted, _ = Milestone.objects.filter(id=options["id"]).delete()
            if not deleted:
                raise CommandError(f"no milestone with id {options['id']}")
            self.stdout.write(f"removed {options['id']}")
        else:
            for m in Milestone.objects.all():
                self.stdout.write(f"{m.id:>3}  {m}")
