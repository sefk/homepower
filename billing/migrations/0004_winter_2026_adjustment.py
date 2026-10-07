"""Winter CCA adjustment from the April 2026 bill (03/18-04/15/2026).

First winter bill after the March 2026 Base Services Charge, so it fills
the gap 0003 left on purpose. Same build-up as the July bill: -PG&E
generation credit + PCIA (both dollar totals spread over the period's
133.9894 net kWh) + the CCA's generation rate (billed as Peninsula Clean
Energy, since renamed WestLight). PG&E's side on the bill, $0.39757 peak
and $0.36757 off-peak less the $0.0814 baseline credit, matches the
Opower winter rates 0003 seeded (0.3162 / 0.2862). Check: 113.5674 peak +
20.422 off-peak kWh at the resulting all-in rates = $40.02, vs. $40.03
billed (PG&E $30.30 less the $0.08 franchise fee, plus $9.81 generation).

Constants are inlined so this keeps meaning what it meant when written.
"""

from datetime import date

from django.db import migrations

BILL_NET_KWH = 133.9894
GEN_CREDIT = 16.47 / BILL_NET_KWH
PCIA = 4.94 / BILL_NET_KWH
CCA_PEAK = 0.07683
CCA_OFFPEAK = 0.05282
NEW_ERA = date(2026, 3, 1)
NOTE = "April 2026 bill"

ADJUSTMENTS = [
    ("winter", "peak", -GEN_CREDIT + PCIA + CCA_PEAK),
    ("winter", "offpeak", -GEN_CREDIT + PCIA + CCA_OFFPEAK),
]


def seed(apps, schema_editor):
    CcaAdjustment = apps.get_model("billing", "CcaAdjustment")
    for season, period, amount in ADJUSTMENTS:
        CcaAdjustment.objects.update_or_create(
            season=season, period=period, effective_from=NEW_ERA,
            defaults={"amount": amount, "note": NOTE},
        )


def unseed(apps, schema_editor):
    CcaAdjustment = apps.get_model("billing", "CcaAdjustment")
    CcaAdjustment.objects.filter(season="winter", effective_from=NEW_ERA, note=NOTE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0003_seed_rates"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
