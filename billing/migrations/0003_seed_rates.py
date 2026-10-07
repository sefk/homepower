"""Seed the rate tables with what billing/rates.py used to hard-code.

Constants are inlined (not imported) so this migration keeps meaning what
it meant when written, however billing/rates.py evolves.
"""

from datetime import date

from django.db import migrations

BILL_NET_KWH = 171.0329
GEN_CREDIT = 17.23 / BILL_NET_KWH
PCIA = 6.31 / BILL_NET_KWH
# WestLight's rate rose mid-cycle: each pair of bill lines is the old rate
# then the new, splitting the 6/15-7/14 cycle at about 7/1. Pricing each
# line's kWh at its own rate reproduces the bill to the cent.
WESTLIGHT_PEAK = 0.14048
WESTLIGHT_OFFPEAK = 0.04778
WESTLIGHT_PEAK_JUL = 0.15036
WESTLIGHT_OFFPEAK_JUL = 0.05251

PRE_2026 = date(2000, 1, 1)
NEW_ERA = date(2026, 3, 1)
WESTLIGHT_JUL = date(2026, 7, 1)

# (season, period, effective_from, rate, source): pre-2026 all-in values
# back-derived from the 2025-26 bills (docs/635-central-energy-analysis-
# gemini.md 4C), then Opower-basis rates from March 2026. Summer is the
# Opower rate (PG&E net usage less baseline credit) seen on the July 2026
# bill; winter is the early-October 2026 Opower rate, provisional from
# March 1 until the pge_rates backfill dates it properly.
RATES = [
    ("summer", "peak", PRE_2026, 0.66, "seed"),
    ("summer", "offpeak", PRE_2026, 0.45, "seed"),
    ("winter", "peak", PRE_2026, 0.625, "seed"),
    ("winter", "offpeak", PRE_2026, 0.571, "seed"),
    ("summer", "peak", NEW_ERA, 0.441, "opower"),
    ("summer", "offpeak", NEW_ERA, 0.318, "opower"),
    ("winter", "peak", NEW_ERA, 0.3162, "opower"),
    ("winter", "offpeak", NEW_ERA, 0.2862, "opower"),
]

# CCA side: -PG&E generation credit + PCIA + WestLight generation. No
# winter row from March 2026 on purpose -- there's no winter bill yet, and
# rates.missing_adjustments() flags the gap until one is entered.
ADJUSTMENTS = [
    ("summer", "peak", PRE_2026, 0.0, "all-in seed rates include the CCA side"),
    ("summer", "offpeak", PRE_2026, 0.0, "all-in seed rates include the CCA side"),
    ("winter", "peak", PRE_2026, 0.0, "all-in seed rates include the CCA side"),
    ("winter", "offpeak", PRE_2026, 0.0, "all-in seed rates include the CCA side"),
    ("summer", "peak", NEW_ERA, -GEN_CREDIT + PCIA + WESTLIGHT_PEAK, "July 2026 bill"),
    ("summer", "offpeak", NEW_ERA, -GEN_CREDIT + PCIA + WESTLIGHT_OFFPEAK, "July 2026 bill"),
    ("summer", "peak", WESTLIGHT_JUL, -GEN_CREDIT + PCIA + WESTLIGHT_PEAK_JUL,
     "July 2026 bill, WestLight 7/1 rate"),
    ("summer", "offpeak", WESTLIGHT_JUL, -GEN_CREDIT + PCIA + WESTLIGHT_OFFPEAK_JUL,
     "July 2026 bill, WestLight 7/1 rate"),
]


def seed(apps, schema_editor):
    UtilityRate = apps.get_model("billing", "UtilityRate")
    CcaAdjustment = apps.get_model("billing", "CcaAdjustment")
    for season, period, eff, rate, source in RATES:
        UtilityRate.objects.get_or_create(
            season=season, period=period, tier=1, effective_from=eff,
            defaults={"rate": rate, "source": source},
        )
    for season, period, eff, amount, note in ADJUSTMENTS:
        CcaAdjustment.objects.get_or_create(
            season=season, period=period, effective_from=eff,
            defaults={"amount": amount, "note": note},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0002_rate_tables"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
