"""Basket and route lines must name the pack the optimiser actually bought.

The regression these tests pin down: the line's ``product_slug``/``label`` used
to come from the first family member with an offer at the store, so a plan of
two singles was reported as the x4 pack (and its $13.16 price) while the cost and
breakdown described the singles.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import pricing
from app.api import basket
from app.schemas import BasketCompareIn, BasketItemIn

ORIGIN = (-31.9523, 115.8613)
SINGLE = SimpleNamespace(id=10, slug="monster-mango-loco-500ml")
FOUR_PACK = SimpleNamespace(id=11, slug="monster-mango-loco-500ml-x4")
STORE = SimpleNamespace(
    id=1,
    name="Spudshed Willetton",
    suburb="WILLETTON",
    lat=ORIGIN[0],
    lon=ORIGIN[1],
    chain=SimpleNamespace(code="spudshed"),
)


def _offer(product: SimpleNamespace, label: str, price_cents: int, pack_count: int) -> pricing.Offer:
    return pricing.Offer(
        product_id=product.id,
        slug=product.slug,
        label=label,
        store_id=STORE.id,
        store_name=STORE.name,
        chain_code=STORE.chain.code,
        suburb=STORE.suburb,
        distance_km=0.1,
        price_cents=price_cents,
        was_price_cents=None,
        promo_kind="none",
        promo_qty=None,
        promo_total_cents=None,
        size_ml=500,
        pack_count=pack_count,
        caffeine_mg_per_100ml=32.0,
        price_scope="store",
        source="scrape",
        observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    # The 4-pack is listed first, the order the live catalogue returned it in:
    # that was enough for the old first-match label to name the wrong product.
    family = [FOUR_PACK, SINGLE]
    offers = {
        SINGLE.id: [_offer(SINGLE, "Monster Mango Loco 500ml", 329, 1)],
        FOUR_PACK.id: [_offer(FOUR_PACK, "Monster Mango Loco 500ml x4", 1316, 4)],
    }
    monkeypatch.setattr(basket, "nearby_stores", lambda *args, **kwargs: [(STORE, 0.1)])
    monkeypatch.setattr(
        basket,
        "_resolve_families",
        lambda session, items: [(item.slug, item.quantity, family) for item in items],
    )
    # collect_offers is the Postgres-only price lookup; the bundles it feeds are
    # what this test is about.
    monkeypatch.setattr(basket, "collect_offers", lambda *args, **kwargs: offers)
    monkeypatch.setattr(basket, "ulp_prices", lambda: {})


def _payload(quantity: int) -> BasketCompareIn:
    return BasketCompareIn(
        items=[BasketItemIn(slug="monster-mango-loco-500ml", quantity=quantity)],
        lat=ORIGIN[0],
        lon=ORIGIN[1],
        radius_km=15,
        include_fuel=False,
    )


def test_basket_line_names_the_single_it_buys(monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch)
    result = basket.basket_compare(_payload(2), session=None)

    line = result.stores[0].items[0]
    assert line.cost_cents == 658
    assert line.breakdown == ["2 x single @ $3.29"]
    assert line.product_slug == SINGLE.slug
    assert line.label == "Monster Mango Loco 500ml"


def test_route_stop_names_the_single_it_buys(monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch)
    result = basket.route_plan(
        "monster-mango-loco-500ml:2", ORIGIN[0], ORIGIN[1], None, None, session=None
    )

    item = result.stops[0].items[0]
    assert item["cost_cents"] == 658
    assert item["breakdown"] == ["2 x single @ $3.29"]
    assert item["product_slug"] == SINGLE.slug
    assert item["label"] == "Monster Mango Loco 500ml"


def test_line_product_follows_the_money_when_a_plan_mixes_packs():
    # prune_bundles currently drops most bulk options, but once it does not, a
    # plan can buy a 4-pack and a single from two products. The line is labelled
    # with the product carrying most of the cost.
    broken = pricing.Bundle(1, 400, 160.0, "single", 10, "monster-500ml", "Monster 500ml")
    pack = pricing.Bundle(4, 300, 640.0, "4-pack", 11, "monster-500ml-x4", "Monster 500ml x4")

    assert basket._line_product([(broken, 1), (pack, 1)]) == ("monster-500ml", "Monster 500ml")
    assert basket._line_product([(broken, 1), (pack, 2)]) == ("monster-500ml-x4", "Monster 500ml x4")


def test_line_product_without_owned_bundles_is_empty():
    # Hand-built bundles (and any caller that never set an owner) still answer.
    assert basket._line_product([(pricing.Bundle(1, 329, 160.0, "single"), 2)]) == (None, None)
