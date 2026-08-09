"""EV charge session derivation (catalog: /ev/).

Walks ev_charge_power_w samples (collectors/tesla.py) and groups
contiguous power>0 runs into sessions. A run breaks on:

- an explicit zero sample -- the car stopped charging, a real known
  reading, not an absence (collectors/tesla.py emits a genuine 0 W for
  an awake-but-idle car, distinct from a failed/asleep poll writing no
  sample at all); or
- a coverage gap between two samples that would otherwise be in the
  same run -- derived from `core.coverage.uncovered()`, never from raw
  timestamp deltas of our own (docs/REVIEW-INSIGHTS.md: coverage is the
  one authority on what counts as a hole).

Cost is integrated per-sample at the TOU rate actually in effect
(billing.rates.rate_for), so a session straddling the 9pm boundary is
priced correctly on both sides. The counterfactual re-prices the same
per-sample energy at that sample's date's off-peak rate -- what
shifting the whole session past 9pm would have cost.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from billing.rates import rate_for, rates_for
from core import coverage
from core.models import Sample, Series

# Sessions are returned most-recent-first, capped here -- the /ev/ view
# renders exactly this list with no further pagination.
MAX_SESSIONS = 50


@dataclass
class ChargeSession:
    start: datetime
    end: datetime
    kwh: float
    actual_cost: float
    counterfactual_cost: float

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def savings(self) -> float:
        """What shifting this session past 9pm would have saved.

        Never negative: rate_for's peak rate is always >= its off-peak
        rate (billing/rates.py), so a sample billed at peak only ever
        costs more than its off-peak counterfactual, and a sample
        already off-peak costs exactly its counterfactual -- summed
        over the session, savings floors at 0.
        """
        return self.actual_cost - self.counterfactual_cost


def _session_cost(run: list[Sample]) -> ChargeSession:
    kwh = actual_cost = counterfactual_cost = 0.0
    for s in run:
        sample_kwh = s.value * s.duration_s / 3600.0 / 1000.0
        kwh += sample_kwh
        actual_cost += sample_kwh * rate_for(s.ts)
        _, offpeak_rate = rates_for(timezone.localtime(s.ts).date())
        counterfactual_cost += sample_kwh * offpeak_rate
    run_end = run[-1].ts + timedelta(seconds=run[-1].duration_s)
    return ChargeSession(
        start=run[0].ts,
        end=run_end,
        kwh=kwh,
        actual_cost=actual_cost,
        counterfactual_cost=counterfactual_cost,
    )


def sessions(series: Series, start: datetime, end: datetime) -> list[ChargeSession]:
    """Charge sessions in [start, end), most-recent-first, capped at MAX_SESSIONS."""
    samples = list(
        Sample.objects.filter(series=series, ts__gte=start, ts__lt=end).order_by("ts")
    )
    gaps = coverage.uncovered(series, start, end)

    runs: list[list[Sample]] = []
    current: list[Sample] = []
    prev_end = None
    gi = 0
    for s in samples:
        if prev_end is not None and current:
            while gi < len(gaps) and gaps[gi][1] <= prev_end:
                gi += 1
            if gi < len(gaps) and gaps[gi][0] < s.ts and gaps[gi][1] > prev_end:
                runs.append(current)
                current = []
        if s.value > 0:
            current.append(s)
        elif current:
            runs.append(current)
            current = []
        prev_end = s.ts + timedelta(seconds=s.duration_s)
    if current:
        runs.append(current)

    result = [_session_cost(run) for run in runs]
    result.sort(key=lambda cs: cs.start, reverse=True)
    return result[:MAX_SESSIONS]
