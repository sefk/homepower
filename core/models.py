"""Canonical storage: sources produce series, series hold samples.

The load-bearing rule (PRD): absence of a sample means *unknown*, never
zero. CoverageSpan is the explicit record of which intervals are known;
anything outside a span is unknown and must never aggregate as zero.
"""

from django.db import models


class Source(models.Model):
    """One upstream system we collect from (Envoy, SolarEdge, ...)."""

    class Kind(models.TextChoices):
        SOLAR = "solar"
        GRID = "grid"
        EV = "ev"
        GAS = "gas"
        DEVICE = "device"  # one appliance or machine, metered on its own

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    poll_interval_s = models.PositiveIntegerField()
    # Finest granularity the vendor provides. Recorded so the UI can render
    # coarse sources as visibly coarse (stepped, not smoothed) — PRD invariant.
    native_resolution_s = models.PositiveIntegerField()
    enabled = models.BooleanField(default=True)

    def __str__(self):
        return self.slug


class Series(models.Model):
    """One metric stream from a source, e.g. envoy/production_w."""

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="series")
    metric = models.CharField(max_length=50)  # e.g. production_w, consumption_w
    unit = models.CharField(max_length=10)  # W, Wh, ...

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "metric"], name="uniq_series")
        ]
        verbose_name_plural = "series"

    def __str__(self):
        return f"{self.source.slug}/{self.metric}"


class Sample(models.Model):
    """One reading at native resolution. ts is the UTC interval start."""

    series = models.ForeignKey(Series, on_delete=models.CASCADE, related_name="samples")
    ts = models.DateTimeField()
    duration_s = models.PositiveIntegerField()
    value = models.FloatField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["series", "ts"], name="uniq_sample")
        ]
        indexes = [models.Index(fields=["series", "ts"], name="idx_sample_series_ts")]

    def __str__(self):
        return f"{self.series}@{self.ts:%Y-%m-%d %H:%M:%S}={self.value}"


class CoverageSpan(models.Model):
    """An interval [start, end) whose data status is known.

    Absence of any span = unknown. `confirmed_empty` means the vendor was
    asked and also has nothing — a healed hole, distinct from a hole.
    """

    class State(models.TextChoices):
        LIVE = "live"  # written by the collector as it ran
        BACKFILLED = "backfilled"  # fetched later from vendor history
        CONFIRMED_EMPTY = "confirmed_empty"  # vendor also has nothing

    series = models.ForeignKey(Series, on_delete=models.CASCADE, related_name="coverage")
    start = models.DateTimeField()
    end = models.DateTimeField()
    state = models.CharField(max_length=20, choices=State.choices)

    class Meta:
        indexes = [
            models.Index(fields=["series", "start"], name="idx_cov_series_start")
        ]

    def __str__(self):
        return f"{self.series} {self.state} [{self.start:%m-%d %H:%M}–{self.end:%m-%d %H:%M})"


class CollectorRun(models.Model):
    """One poll attempt. Feeds the data-health view and staleness checks."""

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="runs")
    started = models.DateTimeField()
    finished = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(null=True)  # null = still running / died mid-run
    message = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["source", "-started"], name="idx_run_source_started")
        ]

    def __str__(self):
        status = {True: "ok", False: "fail", None: "?"}[self.ok]
        return f"{self.source.slug} {self.started:%m-%d %H:%M:%S} {status}"


class Milestone(models.Model):
    """A dated change in how the house uses energy (a new appliance, a
    schedule change), drawn as a marker on the Grafana time charts so a
    step in the data has its cause beside it. Managed with
    `manage.py milestone`."""

    date = models.DateField()  # local calendar day
    label = models.CharField(max_length=200)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"{self.date} {self.label}"
