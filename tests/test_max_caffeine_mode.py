"""Budget mode must describe the plan it actually returns.

The regression these tests pin down: `mode=single_store` seeded the answer with
the unrestricted pool and per-store pools could only tie it, so the endpoint
answered a one-shop question with a plan assembled from several shops.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import basket
from app.main import app as fastapi_app
from app.pricing import Offer

ORIGIN = (-31.9523, 115.8613)


def _store(store_id: int, name: str) -> SimpleNamespace:
    # Only the attributes collect_offers() reads when it is not monkeypatched.
    return SimpleNamespace(
        id=store_id,
        name=name,
        suburb="PERTH",
        lat=ORIGIN[0],
        lon=ORIGIN[1],
        chain=SimpleNamespace(code="coles"),
    )


FIRST = _store(1, "First Coles")
SECOND = _store(2, "Second Coles")


def _product(product_id: int, slug: str, mg_per_100: float = 32.0) -> SimpleNamespace:
    return SimpleNamespace(id=product_id, slug=slug, size_ml=500, caffeine_mg_per_100ml=mg_per_100)


# Each store stocks exactly one of the two drinks, so no single shop can reach
# the unrestricted optimum: 2 x alpha is 320 mg for $18, while alpha + beta is
# 360 mg for $21 inside the $22 budget.
ALPHA = _product(10, "alpha-500ml")
BETA = _product(20, "beta-500ml", 40.0)


def _offer(product: SimpleNamespace, store: SimpleNamespace, price_cents: int) -> Offer:
    # collect_offers() derives both the offer and the knapsack value from the
    # product row, so the fixture keeps them consistent too.
    return Offer(
        product_id=product.id,
        slug=product.slug,
        label=product.slug,
        store_id=store.id,
        store_name=store.name,
        chain_code=store.chain.code,
        suburb=store.suburb,
        distance_km=0.5,
        price_cents=price_cents,
        was_price_cents=None,
        promo_kind="none",
        promo_qty=None,
        promo_total_cents=None,
        size_ml=product.size_ml,
        pack_count=1,
        caffeine_mg_per_100ml=product.caffeine_mg_per_100ml,
        price_scope="store",
        source="scrape",
        observed_at=datetime.now(timezone.utc),
    )


class _StubSession:
    """Enough of a Session for the catalogue query the endpoint runs."""

    def scalars(self, _statement: object) -> list[SimpleNamespace]:
        return [ALPHA, BETA]


def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    offers = {
        ALPHA.id: [_offer(ALPHA, FIRST, 900)],
        BETA.id: [_offer(BETA, SECOND, 1200)],
    }
    monkeypatch.setattr(basket, "nearby_stores", lambda *args, **kwargs: [(FIRST, 0.5), (SECOND, 1.0)])
    monkeypatch.setattr(basket, "collect_offers", lambda *args, **kwargs: offers)


def _plan(monkeypatch: pytest.MonkeyPatch, mode: str):
    _wire(monkeypatch)
    return basket.max_caffeine(
        budget_cents=2200,
        lat=ORIGIN[0],
        lon=ORIGIN[1],
        radius_km=15,
        mode=mode,
        max_age_hours=None,
        include_specials=True,
        session=_StubSession(),
    )


def test_single_store_mode_buys_from_one_shop(monkeypatch: pytest.MonkeyPatch):
    plan = _plan(monkeypatch, "single_store")

    assert plan.mode == "single_store"
    assert plan.stores_used == 1
    assert {item.store_name for item in plan.items} == {FIRST.name}
    # The one-shop answer to the same budget is smaller than the split plan.
    assert plan.total_caffeine_mg == 320
    assert plan.spent_cents == 1800
    assert plan.leftover_cents == 400


def test_multi_store_mode_still_splits_the_budget(monkeypatch: pytest.MonkeyPatch):
    plan = _plan(monkeypatch, "multi_store")

    assert plan.stores_used == 2
    assert {item.store_name for item in plan.items} == {FIRST.name, SECOND.name}
    assert plan.total_caffeine_mg == 360
    assert plan.spent_cents == 2100


def test_single_store_mode_never_beats_multi_store(monkeypatch: pytest.MonkeyPatch):
    single = _plan(monkeypatch, "single_store")
    multi = _plan(monkeypatch, "multi_store")

    assert single.total_caffeine_mg < multi.total_caffeine_mg
    assert single.spent_cents <= multi.spent_cents


def test_single_store_mode_reports_nothing_when_the_only_shop_is_empty(
    monkeypatch: pytest.MonkeyPatch,
):
    _wire(monkeypatch)
    # A store with no stock must not be replaced by the unrestricted pool, which
    # would smuggle the other shop's drinks into a single-store answer.
    monkeypatch.setattr(
        basket, "nearby_stores", lambda *args, **kwargs: [(SimpleNamespace(id=3), 0.5)]
    )
    plan = basket.max_caffeine(
        budget_cents=2200,
        lat=ORIGIN[0],
        lon=ORIGIN[1],
        radius_km=15,
        mode="single_store",
        max_age_hours=None,
        include_specials=True,
        session=_StubSession(),
    )

    assert plan.items == []
    assert plan.stores_used == 0
    assert plan.spent_cents == 0


def test_http_response_serialises_a_single_store_plan(monkeypatch: pytest.MonkeyPatch):
    """Guard the response_model: the endpoint must serialise, not just compute."""
    from app.db import get_session

    _wire(monkeypatch)
    fastapi_app.dependency_overrides[get_session] = lambda: _StubSession()
    try:
        response = TestClient(fastapi_app).get(
            "/v1/value/max-caffeine",
            params={
                "budget_cents": 2200,
                "lat": ORIGIN[0],
                "lon": ORIGIN[1],
                "radius_km": 15,
                "mode": "single_store",
            },
        )
    finally:
        fastapi_app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "single_store"
    assert body["stores_used"] == 1
    assert body["total_caffeine_mg"] == 320
