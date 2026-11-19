"""Basket compare must charge the same drive-and-fuel model as the route planner.

The regression these tests pin down: basket compare used to rank stores on shelf
prices alone, so a plan with more stops always looked cheapest.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import pricing
from app.api import basket
from app.config import settings
from app.schemas import BasketCompareIn, BasketItemIn

ORIGIN = (-31.9523, 115.8613)


def _store(store_id: int, name: str, suburb: str, lat: float, lon: float) -> SimpleNamespace:
    # Only the attributes the endpoint reads; no DB rows needed.
    return SimpleNamespace(
        id=store_id,
        name=name,
        suburb=suburb,
        lat=lat,
        lon=lon,
        chain=SimpleNamespace(code="coles"),
    )


NEAR = _store(1, "Near Coles", "PERTH", -31.9523, 115.8613)
FAR = _store(2, "Far Coles", "WELSHPOOL", -31.9900, 115.9500)

# The far store is 10% cheaper on the shelf but a 3 km drive each way.
PRICES = {"PERTH": 180, "WELSHPOOL": 180}


def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    product = SimpleNamespace(id=10, slug="monster-500ml")
    per_store = {
        NEAR.id: {"monster": [pricing.Bundle(1, 1000, 100.0, "single")]},
        FAR.id: {"monster": [pricing.Bundle(1, 900, 100.0, "single")]},
    }
    monkeypatch.setattr(basket, "nearby_stores", lambda *args, **kwargs: [(NEAR, 0.1), (FAR, 3.0)])
    monkeypatch.setattr(
        basket,
        "_resolve_families",
        lambda session, items: [(item.slug, item.quantity, [product]) for item in items],
    )
    monkeypatch.setattr(basket, "_bundles_by_store", lambda *args, **kwargs: per_store)
    monkeypatch.setattr(basket, "ulp_prices", lambda: dict(PRICES))


def _payload(include_fuel: bool = True) -> BasketCompareIn:
    return BasketCompareIn(
        items=[BasketItemIn(slug="monster", quantity=1)],
        lat=ORIGIN[0],
        lon=ORIGIN[1],
        radius_km=15,
        include_fuel=include_fuel,
    )


def test_drive_distance_applies_factor_and_never_shrinks():
    assert pricing.drive_distance_km(10.0, 1.3) == pytest.approx(13.0)
    # A road cannot beat the straight line, so a sub-1 factor is ignored.
    assert pricing.drive_distance_km(10.0, 0.5) == pytest.approx(10.0)
    assert pricing.drive_distance_km(10.0) == pytest.approx(10.0 * settings.drive_factor)


def test_distance_travelled_stays_straight_line():
    # One degree of latitude is ~111.19 km each way, there and back.
    tour = pricing.distance_travelled([(ORIGIN[0] + 1.0, ORIGIN[1])], ORIGIN)
    assert tour == pytest.approx(2 * 111.19, abs=0.05)


def test_basket_compare_charges_fuel_per_store(monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch)
    result = basket.basket_compare(_payload(), session=None)

    rows = {row.store_id: row for row in result.stores}
    assert rows[NEAR.id].distance_km == 0.1  # straight-line value kept for traceability
    assert rows[NEAR.id].drive_distance_km == pytest.approx(round(2 * 0.1 * settings.drive_factor, 1))
    assert rows[NEAR.id].fuel_cost_cents > 0
    assert rows[NEAR.id].total_with_fuel_cents == rows[NEAR.id].total_cents + rows[NEAR.id].fuel_cost_cents
    assert result.drive_factor == settings.drive_factor
    assert result.include_fuel is True


def test_fuel_flips_the_single_store_recommendation(monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch)
    with_fuel = basket.basket_compare(_payload(include_fuel=True), session=None)
    price_only = basket.basket_compare(_payload(include_fuel=False), session=None)

    # Without fuel the cheaper shelf price wins; with the 3 km each-way detour
    # charged, the nearby store is actually the better plan.
    assert price_only.best_single_store is not None
    assert price_only.best_single_store.store_id == FAR.id
    assert with_fuel.best_single_store is not None
    assert with_fuel.best_single_store.store_id == NEAR.id


def test_multi_store_plan_pays_its_own_detour(monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch)
    result = basket.basket_compare(_payload(), session=None)

    assert result.multi_store_plan, "a plan should still be produced"
    assert result.multi_store_items_cents == 900
    assert result.multi_store_fuel_cost_cents is not None
    assert result.multi_store_fuel_cost_cents > 0
    assert result.multi_store_total_cents == 900 + result.multi_store_fuel_cost_cents
    assert result.multi_store_straight_line_km is not None
    assert result.multi_store_travel_km is not None
    assert result.multi_store_travel_km >= result.multi_store_straight_line_km
    assert result.fuel_price_cents_per_l == 180


def test_multi_store_total_is_items_only_when_fuel_is_opted_out(monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch)
    result = basket.basket_compare(_payload(include_fuel=False), session=None)

    assert result.multi_store_total_cents == 900
    # Fuel is still reported even when the caller keeps the old ranking.
    assert result.multi_store_fuel_cost_cents is not None
    assert result.multi_store_fuel_cost_cents > 0


def test_fuel_quote_takes_cheapest_suburb_then_falls_back():
    cheapest = basket._fuel_quote(10.0, ["PERTH", "WELSHPOOL"], {"PERTH": 180, "WELSHPOOL": 170})
    assert cheapest[0] == 170
    assert cheapest[2] == "fuelwatch"
    assert cheapest[1] == pricing.fuel_cost_cents(10.0, 170)

    fallback = basket._fuel_quote(10.0, ["NOWHERE"], {})
    assert fallback[0] == settings.fuel_price_cents_per_l
    assert fallback[2] == "fallback"


def test_route_plan_reports_both_distances(monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch)
    result = basket.route_plan("monster:1", ORIGIN[0], ORIGIN[1], None, None, session=None)

    assert result.straight_line_km is not None
    assert result.travel_km == pytest.approx(round(result.straight_line_km * settings.drive_factor, 1))
    assert result.fuel_cost_cents > 0


def test_http_response_serialises_the_fuel_fields(monkeypatch: pytest.MonkeyPatch):
    """Guard the response_model: the endpoint must serialise, not just compute."""
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import app as fastapi_app

    _wire(monkeypatch)
    fastapi_app.dependency_overrides[get_session] = lambda: None
    try:
        response = TestClient(fastapi_app).post(
            "/v1/basket/compare",
            json={
                "items": [{"slug": "monster", "quantity": 1}],
                "lat": ORIGIN[0],
                "lon": ORIGIN[1],
                "radius_km": 15,
            },
        )
    finally:
        fastapi_app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["drive_factor"] == settings.drive_factor
    assert body["multi_store_fuel_cost_cents"] > 0
    assert body["best_single_store"]["store_id"] == NEAR.id
    assert body["best_single_store"]["distance_km"] == 0.1
    assert body["best_single_store"]["drive_distance_km"] > 0
