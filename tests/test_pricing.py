"""Unit tests for the pure price optimisers in app.pricing.

Everything here goes in as plain dataclasses and comes back as plain values, so
the suite runs with no database and no network - the property the module
docstring promises when it says the optimisers are kept pure.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.api.deps import haversine_km
from app.pricing import (
    Bundle,
    KnapsackItem,
    Offer,
    cheapest_quantity_plan,
    distance_travelled,
    maximise_caffeine,
    prune_bundles,
)


def make_offer(**overrides: object) -> Offer:
    """An Offer with boring defaults; override only the field under test."""
    fields: dict[str, object] = {
        "product_id": 1,
        "slug": "monster-ultra-500ml",
        "label": "Monster Ultra 500ml",
        "store_id": 10,
        "store_name": "Spudshed Willetton",
        "chain_code": "spudshed",
        "suburb": "Willetton",
        "distance_km": 3.2,
        "price_cents": 329,
        "was_price_cents": None,
        "promo_kind": "none",
        "promo_qty": None,
        "promo_total_cents": None,
        "size_ml": 500,
        "pack_count": 1,
        "caffeine_mg_per_100ml": 32.0,
        "price_scope": "store",
        "source": "scrape",
        "observed_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    return Offer(**fields)  # type: ignore[arg-type]


def make_item(cost_cents: int, caffeine_mg: float, label: str = "item") -> KnapsackItem:
    return KnapsackItem(
        cost_cents=cost_cents,
        caffeine_mg=caffeine_mg,
        label=label,
        store_id=10,
        store_name="Spudshed Willetton",
        chain_code="spudshed",
        slug=label.lower(),
        units=1,
    )


# --- Offer.bundles: the input side of the optimisers -------------------------


def test_offer_bundles_single_can() -> None:
    offer = make_offer()

    assert offer.can_caffeine_mg == pytest.approx(160.0)
    bundles = offer.bundles()

    assert bundles == [Bundle(units=1, cost_cents=329, caffeine_mg=pytest.approx(160.0), label="single")]


def test_offer_bundles_multipack_prices_the_whole_pack() -> None:
    offer = make_offer(pack_count=4, price_cents=1200)

    bundles = offer.bundles()

    assert [bundle.units for bundle in bundles] == [1, 4]
    assert [bundle.cost_cents for bundle in bundles] == [1200, 1200]
    assert bundles[1].label == "4-pack"
    # 32mg/100mL x 500mL = 160mg per can, four cans to the pack.
    assert bundles[1].caffeine_mg == pytest.approx(640.0)


def test_offer_bundles_multibuy_promo() -> None:
    offer = make_offer(promo_kind="multibuy", promo_qty=3, promo_total_cents=900)

    bundles = offer.bundles()

    assert bundles[-1].units == 3
    assert bundles[-1].cost_cents == 900
    assert bundles[-1].caffeine_mg == pytest.approx(480.0)
    assert bundles[-1].label == "3 x 1-pack deal"


def test_offer_bundles_multibuy_without_a_total_is_ignored() -> None:
    offer = make_offer(promo_kind="multibuy", promo_qty=3, promo_total_cents=None)

    assert [bundle.units for bundle in offer.bundles()] == [1]


def test_offer_bundles_ignores_non_multibuy_promos() -> None:
    # A "special" is just a cheaper single price; it must not invent a bundle.
    offer = make_offer(promo_kind="special", promo_qty=3, promo_total_cents=900)

    assert [bundle.units for bundle in offer.bundles()] == [1]


def test_offer_without_a_size_has_no_caffeine() -> None:
    offer = make_offer(size_ml=None)

    assert offer.can_caffeine_mg == 0.0
    assert offer.pack_caffeine_mg == 0.0


# --- prune_bundles -----------------------------------------------------------


def test_prune_bundles_drops_a_dearer_option_for_the_same_cans() -> None:
    cheap = Bundle(1, 250, 160.0, "single")
    dear = Bundle(1, 400, 160.0, "corner store")

    assert prune_bundles([dear, cheap]) == [cheap]


def test_prune_bundles_keeps_a_bulk_option_that_is_cheaper_in_total() -> None:
    single = Bundle(1, 400, 160.0, "single")
    bulk = Bundle(4, 300, 640.0, "4-pack")

    # Neither dominates: the bulk pack has more cans for less money.
    assert prune_bundles([single, bulk]) == [single, bulk]


def test_prune_bundles_keeps_exact_duplicates() -> None:
    # The dominance test is strict about at least one dimension improving, so a
    # duplicate price is left alone rather than silently collapsed.
    bundle = Bundle(2, 500, 320.0, "2-pack")

    assert prune_bundles([bundle, Bundle(2, 500, 320.0, "2-pack")]) == [bundle, bundle]


def test_prune_bundles_sorts_by_units() -> None:
    bulk = Bundle(4, 300, 640.0, "4-pack")
    single = Bundle(1, 400, 160.0, "single")

    assert prune_bundles([bulk, single]) == [single, bulk]


def test_prune_bundles_empty() -> None:
    assert prune_bundles([]) == []


# --- cheapest_quantity_plan --------------------------------------------------


def test_plan_for_no_quantity_is_free() -> None:
    assert cheapest_quantity_plan([Bundle(1, 329, 160.0, "single")], 0) == (0, [])
    assert cheapest_quantity_plan([Bundle(1, 329, 160.0, "single")], -3) == (0, [])


def test_plan_without_bundles_is_none() -> None:
    assert cheapest_quantity_plan([], 2) is None


def test_plan_repeats_the_cheapest_single() -> None:
    single = Bundle(1, 250, 160.0, "single")

    assert cheapest_quantity_plan([single], 3) == (750, [(single, 3)])


def test_plan_ignores_a_dearer_single_in_the_same_store() -> None:
    cheap = Bundle(1, 250, 160.0, "single")
    dear = Bundle(1, 400, 160.0, "single")

    assert cheapest_quantity_plan([dear, cheap], 3) == (750, [(cheap, 3)])


@pytest.mark.xfail(
    strict=True,
    reason=(
        "prune_bundles drops the 4-pack before the DP ever sees it, so the plan "
        "buys four singles for more than the pack costs (see REPORT.md)"
    ),
)
def test_plan_uses_a_bulk_pack_when_it_is_cheaper() -> None:
    single = Bundle(1, 329, 160.0, "single")
    four_pack = Bundle(4, 1200, 640.0, "4-pack")

    plan = cheapest_quantity_plan([single, four_pack], 4)

    assert plan == (1200, [(four_pack, 1)])


# --- maximise_caffeine -------------------------------------------------------


def test_knapsack_without_items_or_budget_is_empty() -> None:
    assert maximise_caffeine([], 1000).items == []
    assert maximise_caffeine([make_item(250, 80.0)], 0).items == []
    assert maximise_caffeine([make_item(250, 80.0)], -100).items == []


def test_knapsack_ignores_free_but_caffeine_free_or_priceless_items() -> None:
    items = [make_item(0, 100.0, "free"), make_item(250, 0.0, "caffeine-free")]

    result = maximise_caffeine(items, 1000)

    assert result.items == []
    assert result.spent_cents == 0
    assert result.caffeine_mg == 0.0


def test_knapsack_repeats_the_best_item_to_fill_the_budget() -> None:
    item = make_item(250, 80.0)

    result = maximise_caffeine([item], 1000)

    assert result.spent_cents == 1000
    assert result.caffeine_mg == pytest.approx(320.0)
    assert result.items == [(item, 4)]


def test_knapsack_leaves_unspendable_change() -> None:
    item = make_item(300, 100.0)

    result = maximise_caffeine([item], 650)

    assert result.spent_cents == 600
    assert result.caffeine_mg == pytest.approx(200.0)
    assert result.items == [(item, 2)]


def test_knapsack_prefers_the_better_total_over_the_better_ratio() -> None:
    # 1.0 mg/cent vs 0.98 mg/cent: the ratio winner still loses on a $5 budget.
    ratio_winner = make_item(300, 300.0, "ratio")
    total_winner = make_item(500, 490.0, "total")

    result = maximise_caffeine([ratio_winner, total_winner], 500)

    assert result.items == [(total_winner, 1)]
    assert result.caffeine_mg == pytest.approx(490.0)


def test_knapsack_never_picks_a_dominated_item() -> None:
    better = make_item(400, 300.0, "better")
    worse = make_item(500, 250.0, "worse")

    result = maximise_caffeine([better, worse], 1000)

    assert result.caffeine_mg == pytest.approx(600.0)
    assert [item for item, _ in result.items] == [better]


def test_knapsack_spend_matches_the_itemised_total() -> None:
    items = [make_item(329, 160.0, "a"), make_item(250, 80.0, "b")]

    result = maximise_caffeine(items, 1000)

    itemised = sum(item.cost_cents * count for item, count in result.items)
    assert itemised == result.spent_cents
    assert result.caffeine_mg == pytest.approx(
        sum(item.caffeine_mg * count for item, count in result.items)
    )
    assert result.spent_cents <= 1000


# --- distance_travelled ------------------------------------------------------


def test_distance_without_stops_is_zero() -> None:
    assert distance_travelled([], (0.0, 0.0)) == 0.0


def test_distance_for_one_stop_is_there_and_back() -> None:
    tour = distance_travelled([(0.0, 1.0)], (0.0, 0.0))

    # One degree of longitude on the equator is ~111.19km each way.
    assert tour == pytest.approx(222.39, rel=1e-4)


def test_distance_visits_nearest_first_and_returns_to_origin() -> None:
    stops = [(0.0, 0.5), (0.0, 1.0), (0.0, 2.0)]

    tour = distance_travelled(stops, (0.0, 0.0))
    out_and_back = 2 * (
        haversine_km(0.0, 0.0, 0.0, 0.5)
        + haversine_km(0.0, 0.5, 0.0, 1.0)
        + haversine_km(0.0, 1.0, 0.0, 2.0)
    )

    assert tour == pytest.approx(out_and_back)
    assert tour == pytest.approx(444.78, rel=1e-4)


def test_distance_does_not_depend_on_input_order_and_leaves_the_list_alone() -> None:
    stops = [(0.0, 2.0), (0.0, 0.5), (0.0, 1.0)]

    tour = distance_travelled(stops, (0.0, 0.0))

    assert tour == pytest.approx(distance_travelled(list(reversed(stops)), (0.0, 0.0)))
    # The caller's list is copied, never consumed.
    assert stops == [(0.0, 2.0), (0.0, 0.5), (0.0, 1.0)]
