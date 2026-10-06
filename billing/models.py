"""Billed totals, transcribed from PG&E/PCE statements — ground truth for
cost analyses until live grid data (Eagle 3) exists. See
docs/635-central-energy-analysis-gemini.md §4 for the source tables.
"""

from django.db import models


class BillPeriod(models.Model):
    """One PG&E/PCE electric bill period (NEM, TOU).

    Values are net over the period; a negative net_kwh/nem_charges means
    the home exported more solar than it consumed from the grid.
    """

    end_date = models.DateField(unique=True)
    peak_kwh = models.FloatField()
    offpeak_kwh = models.FloatField()
    net_kwh = models.FloatField()
    nem_charges = models.FloatField()  # dollars, before taxes; signed

    class Meta:
        ordering = ["end_date"]

    def __str__(self):
        return f"bill ending {self.end_date}"


class GasBillPeriod(models.Model):
    """One PG&E gas bill period (G1 XB). Modeled from bills only — gas
    metering is out of scope (PRD)."""

    start_date = models.DateField()
    end_date = models.DateField(unique=True)
    therms = models.FloatField()
    charges = models.FloatField()

    class Meta:
        ordering = ["end_date"]

    def __str__(self):
        return f"gas {self.start_date}–{self.end_date}"


class Season(models.TextChoices):
    SUMMER = "summer"  # June-September
    WINTER = "winter"  # October-May


class Period(models.TextChoices):
    PEAK = "peak"  # 4-9pm local, every day
    OFFPEAK = "offpeak"


class UtilityRate(models.Model):
    """PG&E's per-kWh rate for a (season, period, tier), effective-dated.

    As Opower reports it: net-usage rate, less the baseline credit on tier
    1. Excludes the CCA side (see CcaAdjustment). Rows with source "seed"
    are the exception: pre-March-2026 all-in values. See billing/rates.py.
    """

    season = models.CharField(max_length=6, choices=Season.choices)
    period = models.CharField(max_length=7, choices=Period.choices)
    tier = models.PositiveSmallIntegerField(default=1)
    effective_from = models.DateField()
    rate = models.FloatField()  # $/kWh
    source = models.CharField(max_length=20)  # "opower" / "bill" / "seed"
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["effective_from", "season", "period", "tier"]
        constraints = [
            models.UniqueConstraint(
                fields=["season", "period", "tier", "effective_from"], name="uniq_utility_rate"
            )
        ]

    def __str__(self):
        return f"{self.season} {self.period} t{self.tier} from {self.effective_from}: {self.rate:.4f}"


class CcaAdjustment(models.Model):
    """What to add to the UtilityRate for the CCA side, $/kWh, effective-dated.

    WestLight generation - PG&E generation credit + PCIA. Hand-entered from
    a bill; Opower doesn't report it.
    """

    season = models.CharField(max_length=6, choices=Season.choices)
    period = models.CharField(max_length=7, choices=Period.choices)
    effective_from = models.DateField()
    amount = models.FloatField()  # $/kWh, may be negative
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["effective_from", "season", "period"]
        constraints = [
            models.UniqueConstraint(
                fields=["season", "period", "effective_from"], name="uniq_cca_adjustment"
            )
        ]

    def __str__(self):
        return f"{self.season} {self.period} from {self.effective_from}: {self.amount:+.4f}"
