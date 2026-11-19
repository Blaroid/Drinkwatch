"""Chain-scope prices must be matched to a store by state, not by chain alone.

Pure functions only: the SQL grouping is exercised against Postgres by hand.
"""

from __future__ import annotations

from app.api.deps import chain_row_for


def test_state_specific_row_wins_over_unscoped() -> None:
    rows = {
        (7, "WA"): {"price_cents": 450},
        (7, None): {"price_cents": 400},
    }

    assert chain_row_for(rows, 7, state="WA") == {"price_cents": 450}


def test_unscoped_row_is_the_legacy_fallback() -> None:
    rows = {(7, None): {"price_cents": 400}}

    assert chain_row_for(rows, 7, state="WA") == {"price_cents": 400}


def test_another_states_row_never_matches() -> None:
    rows = {(7, "NSW"): {"price_cents": 350}}

    assert chain_row_for(rows, 7, state="WA") is None


def test_store_with_unknown_state_does_not_get_a_state_pinned_price() -> None:
    rows = {(7, "WA"): {"price_cents": 450}}

    assert chain_row_for(rows, 7, state=None) is None


def test_product_key_is_part_of_the_lookup() -> None:
    rows = {(7, 3, "WA"): {"price_cents": 450}}

    assert chain_row_for(rows, 7, 3, state="WA") == {"price_cents": 450}
    assert chain_row_for(rows, 7, 4, state="WA") is None
