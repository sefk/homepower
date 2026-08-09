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
