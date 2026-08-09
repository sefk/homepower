"""Historical bill data, transcribed verbatim from
docs/635-central-energy-analysis-gemini.md §4. Ground truth — don't
"clean up" the numbers, even where a negative value looks odd (it's a
NEM export credit, not an error).
"""

from datetime import date

# (end_date, peak_kwh, offpeak_kwh, net_kwh, nem_charges)
#
# The source table's "2025-2026 True-Up Totals" row is a summary of the
# twelve rows above it, not a bill period, and is intentionally omitted.
ELECTRIC_BILLS = [
    (date(2025, 5, 13), 3, -436, -433, -120.23),
    (date(2025, 6, 15), -182, -861, -1043, -343.43),
    (date(2025, 7, 15), -136, -588, -724, -249.07),
    (date(2025, 8, 13), -58, -357, -415, -130.57),
    (date(2025, 9, 14), 51, -232, -181, -49.37),
    (date(2025, 10, 14), 147, -184, -37, -8.42),
    (date(2025, 11, 13), 323, 207, 530, 148.42),
    (date(2025, 12, 15), 474, 563, 1037, 316.27),
    (date(2026, 1, 14), 482, 913, 1395, 464.64),
    (date(2026, 2, 16), 453, 508, 960, 327.37),
    (date(2026, 3, 17), 209, 7, 216, 72.43),
    (date(2026, 4, 15), 114, 20, 134, 30.22),
    (date(2026, 5, 14), -9, -171, -180, -40.05),
    (date(2026, 6, 14), -39, -447, -486, -128.89),
    (date(2026, 7, 14), -10, -161, -171, -44.69),
]

# (start_date, end_date, therms, charges)
GAS_BILLS = [
    (date(2025, 10, 16), date(2025, 11, 14), 25.0, 70.84),
    (date(2025, 11, 15), date(2025, 12, 16), 91.0, 279.86),
    (date(2025, 12, 17), date(2026, 1, 15), 74.0, 219.24),
    (date(2026, 1, 16), date(2026, 2, 17), 94.0, 267.33),
    (date(2026, 2, 18), date(2026, 3, 18), 30.0, 74.42),
    (date(2026, 3, 19), date(2026, 4, 16), 17.0, 41.43),
    (date(2026, 4, 17), date(2026, 5, 15), 16.0, 38.95),
    (date(2026, 5, 16), date(2026, 6, 15), 9.0, 22.11),
    (date(2026, 6, 16), date(2026, 7, 15), 10.0, 25.56),
]
