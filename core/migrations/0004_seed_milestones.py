from datetime import date

from django.db import migrations

MILESTONES = [
    (date(2024, 11, 15), "Electric dryer installed"),
    (date(2025, 9, 1), "Sef retired"),
    (date(2026, 9, 21), "Heat pump hot water heater"),
]


def seed(apps, schema_editor):
    Milestone = apps.get_model("core", "Milestone")
    for day, label in MILESTONES:
        Milestone.objects.get_or_create(date=day, label=label)


def unseed(apps, schema_editor):
    Milestone = apps.get_model("core", "Milestone")
    for day, label in MILESTONES:
        Milestone.objects.filter(date=day, label=label).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0003_milestone")]
    operations = [migrations.RunPython(seed, unseed)]
