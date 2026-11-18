"""Public read API: what the Android app and widget call."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..config import settings
from ..db import get_session
from ..models import Chain, Observation, Product, Store
from ..schemas import (
    ChainOut,
    MetaOut,
    OfferOut,
    ProductOut,
    ProductPricesOut,
    StoreOut,
    WidgetItemOut,
    WidgetOut,
)
from .deps import (
    age_hours,
    clamp_radius,
    default_since,
    format_cents,
    get_product_by_barcode,
    get_product_by_slug,
    latest_prices,
    nearby_stores,
    product_group_ids,
    utcnow,
)

router = APIRouter(prefix="/v1", tags=["public"])


def _label(product: Product) -> str:
    bits = [product.brand, product.name]
    if product.variant:
        bits.append(product.variant)
    label = " ".join(b for b in bits if b).strip()
    if product.size_ml:
        size = f"{product.size_ml}ml"
        label = f"{label} {size}" if product.pack_count == 1 else f"{label} {size} x{product.pack_count}"
    return label


def _offers_for_product(
    session: Session,
    product: Product,
    store_distances: list[tuple[Store, float]],
    since: datetime,
) -> list[OfferOut]:
    stores = [store for store, _ in store_distances]
    observations = latest_prices(session, stores, product_group_ids(session, product), since)
    by_id = {store.id: (store, distance) for store, distance in store_distances}
    now = utcnow()
    offers: list[OfferOut] = []
    for store_id, obs in observations.items():
        store, distance = by_id.get(store_id, (None, None))
        if store is None:
            continue
        per_can = (
            round(obs.price_cents / product.pack_count) if product.pack_count > 1 else None
        )
        per_litre = obs.unit_price_cents_per_l
        if per_litre is None and product.size_ml and product.pack_count:
            litres = product.size_ml * product.pack_count / 1000
            per_litre = round(obs.price_cents / litres) if litres > 0 else None
        offers.append(
            OfferOut(
                store_id=store.id,
                store_name=store.name,
                chain_code=store.chain.code,
                chain_name=store.chain.name,
                chain_kind=store.chain.kind,
                suburb=store.suburb,
                distance_km=distance,
                price_cents=obs.price_cents,
                price=format_cents(obs.price_cents),
                was_price_cents=obs.was_price_cents,
                per_can_cents=per_can,
                per_litre_cents=per_litre,
                promo_kind=obs.promo_kind,
                promo_qty=obs.promo_qty,
                promo_total_cents=obs.promo_total_cents,
                in_stock=obs.in_stock,
                source=obs.source,
                price_scope=obs.price_scope,
                age_hours=age_hours(obs.observed_at, now),
                observed_at=obs.observed_at,
            )
        )
    offers.sort(key=lambda offer: (offer.price_cents, offer.distance_km or 999))
    return offers


@router.get("/meta", response_model=MetaOut, summary="Service metadata and data freshness")
def meta(session: Session = Depends(get_session)) -> MetaOut:
    chains = list(session.scalars(select(Chain).order_by(Chain.name)))
    return MetaOut(
        name="drinkwatch",
        version=__version__,
        time=utcnow(),
        chains=[ChainOut.model_validate(chain) for chain in chains],
        store_count=session.scalar(select(func.count()).select_from(Store)) or 0,
        product_count=session.scalar(select(func.count()).select_from(Product)) or 0,
        observation_count=session.scalar(select(func.count()).select_from(Observation)) or 0,
        freshest_observation_at=session.scalar(select(func.max(Observation.observed_at))),
    )


@router.get("/chains", response_model=list[ChainOut])
def chains(session: Session = Depends(get_session)) -> list[ChainOut]:
    return [
        ChainOut.model_validate(chain)
        for chain in session.scalars(select(Chain).order_by(Chain.name))
    ]


@router.get("/stores/nearby", response_model=list[StoreOut])
def stores_nearby(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_km: float | None = Query(default=None, gt=0),
    chain: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[StoreOut]:
    radius = clamp_radius(radius_km)
    result = []
    for store, distance in nearby_stores(session, lat, lon, radius, chain, limit=limit):
        item = StoreOut.model_validate(store)
        item.distance_km = distance
        result.append(item)
    return result


@router.get("/products/barcode/{barcode}", response_model=ProductOut)
def product_by_barcode(barcode: str, session: Session = Depends(get_session)) -> ProductOut:
    return ProductOut.model_validate(get_product_by_barcode(session, barcode))


@router.get("/products", response_model=list[ProductOut])
def products(
    q: str | None = Query(default=None, max_length=80),
    brand: str | None = None,
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[ProductOut]:
    stmt = select(Product).where(Product.is_active.is_(True))
    if q:
        needle = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Product.brand + " " + Product.name + " " + func.coalesce(Product.variant, "")).like(needle)
        )
    if brand:
        stmt = stmt.where(func.lower(Product.brand) == brand.lower())
    if category:
        stmt = stmt.where(Product.category == category)
    stmt = stmt.order_by(Product.brand, Product.name, Product.pack_count).offset(offset).limit(limit)
    return [ProductOut.model_validate(p) for p in session.scalars(stmt)]


@router.get("/products/{slug}", response_model=ProductOut)
def product_detail(slug: str, session: Session = Depends(get_session)) -> ProductOut:
    return ProductOut.model_validate(get_product_by_slug(session, slug))


@router.get("/prices", response_model=ProductPricesOut, summary="Current prices for one product near a point")
def prices(
    product: str = Query(description="product slug"),
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_km: float | None = Query(default=None, gt=0),
    max_age_hours: int | None = Query(default=None, ge=1, le=720),
    chain: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> ProductPricesOut:
    found = get_product_by_slug(session, product)
    radius = clamp_radius(radius_km)
    since = default_since(max_age_hours or settings.default_max_age_hours)
    stores = nearby_stores(session, lat, lon, radius, chain, limit=limit)
    offers = _offers_for_product(session, found, stores, since)
    return ProductPricesOut(
        product=ProductOut.model_validate(found),
        cheapest=offers[0] if offers else None,
        offers=offers,
    )


@router.get("/compare", response_model=list[ProductPricesOut], summary="Current prices for several products")
def compare(
    products: str = Query(description="comma separated product slugs (max 12)"),
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_km: float | None = Query(default=None, gt=0),
    max_age_hours: int | None = Query(default=None, ge=1, le=720),
    chain: str | None = None,
    session: Session = Depends(get_session),
) -> list[ProductPricesOut]:
    slugs = [s.strip() for s in products.split(",") if s.strip()][:12]
    radius = clamp_radius(radius_km)
    since = default_since(max_age_hours or settings.default_max_age_hours)
    stores = nearby_stores(session, lat, lon, radius, chain, limit=200)
    out: list[ProductPricesOut] = []
    for slug in slugs:
        found = get_product_by_slug(session, slug)
        offers = _offers_for_product(session, found, stores, since)
        out.append(
            ProductPricesOut(
                product=ProductOut.model_validate(found),
                cheapest=offers[0] if offers else None,
                offers=offers,
            )
        )
    return out


@router.get("/widget", response_model=WidgetOut, summary="Smallest possible payload for a phone widget")
def widget(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    products: str | None = Query(default=None, description="comma separated slugs (max 8)"),
    radius_km: float | None = Query(default=None, gt=0),
    max_age_hours: int | None = Query(default=None, ge=1, le=720),
    session: Session = Depends(get_session),
) -> WidgetOut:
    radius = clamp_radius(radius_km)
    since = default_since(max_age_hours or settings.default_max_age_hours)

    if products:
        slugs = [s.strip() for s in products.split(",") if s.strip()][:8]
        catalogue = [get_product_by_slug(session, slug) for slug in slugs]
    else:
        # No explicit list: most-observed (i.e. most stocked) energy drinks.
        popularity = (
            select(Observation.product_id, func.count().label("hits"))
            .where(Observation.observed_at >= since)
            .group_by(Observation.product_id)
            .subquery()
        )
        catalogue = list(
            session.scalars(
                select(Product)
                .join(popularity, popularity.c.product_id == Product.id)
                .where(Product.is_active.is_(True), Product.category == "energy")
                .order_by(popularity.c.hits.desc(), Product.brand, Product.name)
                .limit(8)
            )
        )

    stores = nearby_stores(session, lat, lon, radius, limit=200)
    items: list[WidgetItemOut] = []
    for found in catalogue:
        offers = _offers_for_product(session, found, stores, since)
        if not offers:
            continue
        best = offers[0]
        items.append(
            WidgetItemOut(
                slug=found.slug,
                label=_label(found),
                price_cents=best.price_cents,
                price=best.price,
                per_can_cents=best.per_can_cents,
                store=best.store_name,
                chain=best.chain_code,
                suburb=best.suburb,
                distance_km=best.distance_km,
                promo=best.promo_kind,
                source=best.source,
                age_hours=best.age_hours,
            )
        )
    return WidgetOut(generated_at=utcnow(), radius_km=radius, items=items)


@router.get("/observations", response_model=list[dict], summary="Recent price sightings")
def observations(
    product: str | None = None,
    store_id: int | None = None,
    source: str | None = Query(default=None, pattern="^(scrape|crowd)$"),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(Observation).where(Observation.status != "rejected")
    if product:
        found = get_product_by_slug(session, product)
        stmt = stmt.where(Observation.product_id == found.id)
    if store_id:
        stmt = stmt.where(Observation.store_id == store_id)
    if source:
        stmt = stmt.where(Observation.source == source)
    stmt = stmt.order_by(Observation.observed_at.desc()).limit(limit)
    rows = list(session.scalars(stmt))
    store_ids = {r.store_id for r in rows if r.store_id is not None}
    stores = {s.id: s for s in session.scalars(select(Store).where(Store.id.in_(store_ids)))}
    prods = {p.id: p for p in session.scalars(select(Product).where(Product.id.in_({r.product_id for r in rows})))}
    return [
        {
            "id": row.id,
            "product": prods[row.product_id].slug if row.product_id in prods else None,
            "store": stores[row.store_id].name if row.store_id in stores else None,
            "chain": (
                stores[row.store_id].chain.code
                if row.store_id in stores
                else session.scalar(select(Chain.code).where(Chain.id == row.chain_id))
            ),
            "price_cents": row.price_cents,
            "price": format_cents(row.price_cents),
            "promo_kind": row.promo_kind,
            "in_stock": row.in_stock,
            "source": row.source,
            "price_scope": row.price_scope,
            "observed_at": row.observed_at,
            "note": row.note,
        }
        for row in rows
    ]
