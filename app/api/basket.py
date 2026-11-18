"""Basket comparison, budget optimiser and multi-store route planning."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import Product
from ..pricing import (
    Bundle,
    KnapsackItem,
    cheapest_quantity_plan,
    collect_offers,
    distance_travelled,
    family_products,
    maximise_caffeine,
)
from ..schemas import (
    BasketCompareIn,
    BasketCompareOut,
    BasketItemIn,
    BasketLineOut,
    BasketStoreOut,
    BudgetItemOut,
    MaxCaffeineOut,
    RoutePlanOut,
    RouteStopOut,
)
from ..scrape.fuelwatch import price_for_suburb
from .deps import clamp_radius, default_since, format_cents, nearby_stores, utcnow

router = APIRouter(prefix="/v1", tags=["plans"])


def _resolve_families(session: Session, items: list[BasketItemIn]):
    resolved = []
    for item in items:
        products = family_products(session, item.slug)
        if not products:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown product or family: {item.slug}")
        resolved.append((item.slug, item.quantity, products))
    return resolved


def _bundles_by_store(session, stores, families, since, distances):
    products = [product for _, _, family in families for product in family]
    offers = collect_offers(session, stores, products, since, distances)
    per_store: dict[int, dict[str, list[Bundle]]] = {}
    for store in stores:
        for slug, _, family in families:
            bundles: list[Bundle] = []
            for product in family:
                for offer in offers.get(product.id, []):
                    if offer.store_id == store.id:
                        bundles.extend(offer.bundles())
            if bundles:
                per_store.setdefault(store.id, {})[slug] = bundles
    return per_store, offers


def _label_for(family, offers, store_id):
    for product in family:
        for offer in offers.get(product.id, []):
            if offer.store_id == store_id:
                return product.slug, offer.label
    return None, None


@router.post(
    "/basket/compare",
    response_model=BasketCompareOut,
    summary="Price a whole shopping list at each nearby store",
)
def basket_compare(payload: BasketCompareIn, session: Session = Depends(get_session)) -> BasketCompareOut:
    radius = clamp_radius(payload.radius_km)
    since = default_since(payload.max_age_hours or settings.default_max_age_hours)
    store_pairs = nearby_stores(session, payload.lat, payload.lon, radius, limit=200)
    stores = [store for store, _ in store_pairs]
    distances = {store.id: distance for store, distance in store_pairs}
    families = _resolve_families(session, payload.items)
    per_store, offers = _bundles_by_store(session, stores, families, since, distances)

    store_results: list[BasketStoreOut] = []
    for store in stores:
        available = per_store.get(store.id, {})
        if not available:
            continue
        lines: list[BasketLineOut] = []
        total = 0
        complete = True
        for slug, quantity, family in families:
            bundles = available.get(slug)
            plan = cheapest_quantity_plan(bundles, quantity) if bundles else None
            if plan is None:
                complete = False
                lines.append(BasketLineOut(requested=slug, quantity=quantity))
                continue
            cost, breakdown = plan
            units = sum(bundle.units * count for bundle, count in breakdown)
            product_slug, label = _label_for(family, offers, store.id)
            lines.append(
                BasketLineOut(
                    requested=slug,
                    quantity=quantity,
                    product_slug=product_slug,
                    label=label,
                    units=units,
                    cost_cents=cost,
                    price=format_cents(cost),
                    breakdown=[
                        f"{count} x {bundle.label} @ {format_cents(bundle.cost_cents)}"
                        for bundle, count in breakdown
                    ],
                )
            )
            total += cost
        store_results.append(
            BasketStoreOut(
                store_id=store.id,
                store_name=store.name,
                chain_code=store.chain.code,
                suburb=store.suburb,
                distance_km=distances.get(store.id, 0.0),
                total_cents=total,
                total=format_cents(total),
                complete=complete,
                items=lines,
            )
        )

    store_results.sort(key=lambda row: (not row.complete, row.total_cents))

    multi_plan: list[dict] = []
    multi_total = 0
    for slug, quantity, family in families:
        best: tuple[int, int, str, str] | None = None
        for store in stores:
            bundles = per_store.get(store.id, {}).get(slug)
            plan = cheapest_quantity_plan(bundles, quantity) if bundles else None
            if plan is None:
                continue
            cost = plan[0]
            if best is None or cost < best[0]:
                best = (cost, store.id, store.name, store.chain.code)
        if best is not None:
            multi_total += best[0]
            multi_plan.append(
                {
                    "requested": slug,
                    "quantity": quantity,
                    "store_id": best[1],
                    "store_name": best[2],
                    "chain_code": best[3],
                    "cost_cents": best[0],
                    "cost": format_cents(best[0]),
                }
            )

    complete_prices = [row.total_cents for row in store_results if row.complete]
    return BasketCompareOut(
        generated_at=utcnow(),
        radius_km=radius,
        stores=store_results[:25],
        best_single_store=store_results[0] if store_results and store_results[0].complete else None,
        multi_store_total_cents=multi_total or None,
        multi_store_plan=multi_plan,
        savings_vs_worst_cents=(max(complete_prices) - min(complete_prices))
        if len(complete_prices) > 1
        else None,
    )


@router.get(
    "/value/max-caffeine",
    response_model=MaxCaffeineOut,
    summary="Most caffeine (i.e. most millilitres) for a budget",
)
def max_caffeine(
    budget_cents: int = Query(ge=100, le=200000),
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_km: float | None = Query(default=None, gt=0),
    mode: str = Query(default="single_store", pattern="^(single_store|multi_store)$"),
    max_age_hours: int | None = Query(default=None, ge=1, le=720),
    include_specials: bool = Query(default=True, description="include promo/multibuy bundles"),
    session: Session = Depends(get_session),
) -> MaxCaffeineOut:
    if budget_cents > settings.max_budget_cents:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "budget above the configured maximum of "
            + format_cents(settings.max_budget_cents),
        )
    radius = clamp_radius(radius_km)
    since = default_since(max_age_hours or settings.default_max_age_hours)
    store_pairs = nearby_stores(session, lat, lon, radius, limit=60)
    stores = [store for store, _ in store_pairs]
    distances = {store.id: distance for store, distance in store_pairs}

    products = list(
        session.scalars(
            select(Product).where(
                Product.is_active.is_(True),
                Product.category == "energy",
                Product.status != "rejected",
                Product.size_ml.is_not(None),
            )
        )
    )
    if not products:
        raise HTTPException(status.HTTP_409_CONFLICT, "catalogue is empty")

    offers = collect_offers(session, stores, products, since, distances)
    meta = {
        product.slug: (product.size_ml or 0, float(product.caffeine_mg_per_100ml or 0))
        for product in products
    }

    def items_for(allowed_store: int | None = None) -> list[KnapsackItem]:
        pool: list[KnapsackItem] = []
        for product in products:
            for offer in offers.get(product.id, []):
                if allowed_store is not None and offer.store_id != allowed_store:
                    continue
                for bundle in offer.bundles():
                    if not include_specials and bundle.label.endswith("deal"):
                        continue
                    pool.append(
                        KnapsackItem(
                            cost_cents=bundle.cost_cents,
                            caffeine_mg=bundle.caffeine_mg,
                            label=f"{offer.label} ({bundle.label})",
                            store_id=offer.store_id,
                            store_name=offer.store_name,
                            chain_code=offer.chain_code,
                            slug=offer.slug,
                            units=bundle.units,
                        )
                    )
        return pool

    best = maximise_caffeine(items_for(), budget_cents)
    if mode == "single_store":
        for store in stores:
            candidate = maximise_caffeine(items_for(store.id), budget_cents)
            if candidate.caffeine_mg > best.caffeine_mg:
                best = candidate

    output_items: list[BudgetItemOut] = []
    total_ml = 0
    total_mg = 0.0
    store_ids: set[int] = set()
    for item, count in best.items:
        units = item.units * count
        size_ml, caffeine_per_100 = meta.get(item.slug, (0, 0.0))
        ml = size_ml * units
        mg = caffeine_per_100 * ml / 100.0
        total_ml += ml
        total_mg += mg
        store_ids.add(item.store_id)
        output_items.append(
            BudgetItemOut(
                slug=item.slug,
                label=item.label,
                store_id=item.store_id,
                store_name=item.store_name,
                chain_code=item.chain_code,
                units=units,
                cost_cents=item.cost_cents * count,
                cost=format_cents(item.cost_cents * count),
                caffeine_mg=int(round(mg)),
            )
        )

    return MaxCaffeineOut(
        budget_cents=budget_cents,
        mode=mode,
        spent_cents=best.spent_cents,
        leftover_cents=budget_cents - best.spent_cents,
        total_caffeine_mg=int(round(total_mg)),
        total_ml=total_ml,
        items=output_items,
        note=(
            "Caffeine assumes 32mg/100mL unless a product overrides it (FSANZ 2.6.4 cap), "
            "so this ranking is effectively millilitres per dollar."
        ),
        daily_limit_mg=settings.daily_caffeine_limit_mg,
        exceeds_daily_limit=total_mg > settings.daily_caffeine_limit_mg,
        stores_used=len(store_ids),
    )


@router.get("/plan/route", response_model=RoutePlanOut, summary="Multi-store plan with fuel cost")
def route_plan(
    items: str = Query(description="comma separated slug:quantity pairs"),
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_km: float | None = Query(default=None, gt=0),
    max_age_hours: int | None = Query(default=None, ge=1, le=720),
    session: Session = Depends(get_session),
) -> RoutePlanOut:
    parsed: list[BasketItemIn] = []
    for chunk in [part for part in items.split(",") if part.strip()]:
        slug, _, qty = chunk.partition(":")
        try:
            quantity = int(qty) if qty else 1
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"bad quantity in '{chunk}'")
        parsed.append(BasketItemIn(slug=slug.strip(), quantity=max(1, min(quantity, 48))))
    if not parsed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no items requested")

    radius = clamp_radius(radius_km)
    since = default_since(max_age_hours or settings.default_max_age_hours)
    store_pairs = nearby_stores(session, lat, lon, radius, limit=200)
    stores = [store for store, _ in store_pairs]
    by_id = {store.id: store for store in stores}
    distances = {store.id: distance for store, distance in store_pairs}
    families = _resolve_families(session, parsed)
    per_store, offers = _bundles_by_store(session, stores, families, since, distances)

    stops: dict[int, dict] = {}
    for slug, quantity, family in families:
        best = None
        for store in stores:
            bundles = per_store.get(store.id, {}).get(slug)
            plan = cheapest_quantity_plan(bundles, quantity) if bundles else None
            if plan is None:
                continue
            cost, breakdown = plan
            if best is None or cost < best[0]:
                best = (cost, store.id, breakdown)
        if best is None:
            continue
        cost, store_id, breakdown = best
        entry = stops.setdefault(store_id, {"items": [], "subtotal": 0})
        product_slug, label = _label_for(family, offers, store_id)
        entry["items"].append(
            {
                "requested": slug,
                "quantity": quantity,
                "product_slug": product_slug,
                "label": label,
                "cost_cents": cost,
                "cost": format_cents(cost),
                "breakdown": [
                    f"{count} x {bundle.label} @ {format_cents(bundle.cost_cents)}"
                    for bundle, count in breakdown
                ],
            }
        )
        entry["subtotal"] += cost

    stop_rows: list[RouteStopOut] = []
    points: list[tuple[float, float]] = []
    for store_id, entry in sorted(stops.items(), key=lambda pair: distances.get(pair[0], 0.0)):
        store = by_id[store_id]
        if store.lat is not None and store.lon is not None:
            points.append((store.lat, store.lon))
        stop_rows.append(
            RouteStopOut(
                store_id=store.id,
                store_name=store.name,
                chain_code=store.chain.code,
                suburb=store.suburb,
                lat=store.lat,
                lon=store.lon,
                subtotal_cents=entry["subtotal"],
                subtotal=format_cents(entry["subtotal"]),
                items=entry["items"],
                distance_from_origin_km=distances.get(store.id),
            )
        )

    travel_km = round(distance_travelled(points, (lat, lon)), 1) if points else 0.0
    fuel_cents_per_l: int | None = None
    fuel_source: str | None = None
    for stop in stop_rows:
        price = price_for_suburb(stop.suburb)
        if price:
            fuel_cents_per_l = price if fuel_cents_per_l is None else min(fuel_cents_per_l, price)
    if fuel_cents_per_l is None and settings.fuelwatch_enabled:
        fuel_cents_per_l = settings.fuel_price_cents_per_l
        fuel_source = "fallback"
    if fuel_cents_per_l:
        fuel_source = fuel_source or "fuelwatch"
    fuel_cost = (
        round(travel_km * settings.vehicle_l_per_100km / 100 * fuel_cents_per_l)
        if fuel_cents_per_l
        else 0
    )
    items_total = sum(row.subtotal_cents for row in stop_rows)

    baseline: int | None = None
    for store in stores:
        available = per_store.get(store.id, {})
        if not available:
            continue
        total = 0
        for slug, quantity, _family in families:
            bundles = available.get(slug)
            plan = cheapest_quantity_plan(bundles, quantity) if bundles else None
            if plan is None:
                total = -1
                break
            total += plan[0]
        if total >= 0 and (baseline is None or total < baseline):
            baseline = total

    total_cost = items_total + fuel_cost
    return RoutePlanOut(
        generated_at=utcnow(),
        origin={"lat": lat, "lon": lon},
        stops=stop_rows,
        items_total_cents=items_total,
        travel_km=travel_km,
        fuel_price_cents_per_l=fuel_cents_per_l,
        fuel_cost_cents=fuel_cost,
        total_cost_cents=total_cost,
        single_store_baseline_cents=baseline,
        net_saving_cents=(baseline - total_cost) if baseline is not None else None,
        fuel_source=fuel_source,
    )
