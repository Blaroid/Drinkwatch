"""Unit tests for the deal badge shown on public offer rows.

_classify_deal is a pure function of the price and the chain's 90-day stats, so
it is covered directly rather than through an HTTP route (which would need a
database).
"""

from __future__ import annotations

import pytest

from app.api.public import _classify_deal


@pytest.mark.parametrize(
    ("price_cents", "median_cents", "min_cents", "expected"),
    [
        # No history: never guess a badge.
        (900, None, None, "unknown"),
        (900, 0, 0, "unknown"),
        # At or below 105% of the 90-day minimum is "great".
        (840, 1000, 800, "great"),
        (800, 1000, 800, "great"),
        # Between the minimum band and 5% under the median.
        (841, 1000, 800, "good"),
        (950, 1000, 800, "good"),
        # Within +/-10% of the median is "typical".
        (951, 1000, 800, "typical"),
        (1100, 1000, 800, "typical"),
        # Anything dearer than 110% of the median.
        (1101, 1000, 800, "high"),
        # min_cents missing just skips the "great" band.
        (800, 1000, None, "good"),
        (960, 1000, None, "typical"),
        (1050, 1000, None, "typical"),
    ],
)
def test_classify_deal(
    price_cents: int, median_cents: int | None, min_cents: int | None, expected: str
) -> None:
    assert _classify_deal(price_cents, median_cents, min_cents) == expected


def test_classify_deal_tolerates_a_zero_minimum() -> None:
    # `min_cents` is truthiness-checked, so a 0 from an empty aggregate must not
    # make every price look like a record low.
    assert _classify_deal(1000, 1000, 0) == "typical"
