"""April-to-April true-up cycle grouping for BillPeriod data.

PG&E/PCE NEM true-up closes annually with the bill whose period ends in
April; the next period starts a new cycle (PRD: true-up tracker runs the
April cycle). Only the boundary rule lives here — nothing about the
values themselves is assumed or recomputed.
"""

from billing.models import BillPeriod


def true_up_cycles(periods: list[BillPeriod]) -> list[list[BillPeriod]]:
    """Group BillPeriods (ordered by end_date, ascending) into cycles.

    A cycle ends with the period whose end_date falls in April; the next
    period starts a new cycle. The first or last cycle may be incomplete
    if the data doesn't span a full April boundary — callers decide what
    "complete" means (e.g. "not the last cycle in the list").
    """
    cycles = []
    current: list[BillPeriod] = []
    for period in periods:
        current.append(period)
        if period.end_date.month == 4:
            cycles.append(current)
            current = []
    if current:
        cycles.append(current)
    return cycles


def cycle_label(cycle: list[BillPeriod]) -> str:
    """e.g. '2025–26' for a cycle whose first period ends May 2025."""
    year0 = cycle[0].end_date.year
    return f"{year0}–{(year0 + 1) % 100:02d}"
