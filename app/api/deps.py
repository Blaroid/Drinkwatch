"""Shared query helpers for the HTTP layer."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from ..models import Observation, Product, Store


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def nearby_stores(
    session: Session,
    lat: float,
    lon: float,
    radius_km: float,
    chain_code: str | None = None,
    limit: int = 300,
) -> list[tuple[Store, float]]:
    """Stores within radius, nearest first."""
    dlat = radius_km / 111.0
    dlon = radius_km / max(0.01, 111.0 * math.cos(math.radians(lat)))
    stmt = (
        select(Store)
        .where(
            Store.is_active.is_(True),
            Store.lat.is_not(None),
            Store.lon.is_not(None),
            Store.lat.between(lat - dlat, lat + dlat),
            Store.lon.between(lon - dlon, lon + dlon),
        )
        .limit(limit * 4)
    )
    if chain_code:
        from ..models import Chain

        stmt = stmt.join(Chain).where(Chain.code == chain_code)
    found: list[tuple[Store, float]] = []
    for store in session.scalars(stmt):
        distance = haversine_km(lat, lon, store.lat, store.lon)  # type: ignore[arg-type]
        if distance <= radius_km:
            found.append((store, round(distance, 2)))
    found.sort(key=lambda pair: pair[1])
    return found[:limit]


_STORE_SCOPE_SQL = text(
    """
    select distinct on (o.store_id)
           o.id, o.store_id, o.chain_id, o.product_id, o.price_cents, o.was_price_cents,
           o.unit_price_cents_per_l, o.promo_kind, o.promo_qty, o.promo_total_cents,
           o.in_stock, o.source, o.price_scope, o.observed_at
    from observations o
    where o.store_id in :store_ids
      and o.product_id in :product_ids
      and o.status = 'accepted'
      and o.observed_at >= :since
    order by o.store_id, o.observed_at desc, o.id desc
    """
).bindparams(bindparam("store_ids", expanding=True), bindparam("product_ids", expanding=True))

_CHAIN_SCOPE_SQL = text(
    """
    select distinct on (o.chain_id)
           o.id, o.store_id, o.chain_id, o.product_id, o.price_cents, o.was_price_cents,
           o.unit_price_cents_per_l, o.promo_kind, o.promo_qty, o.promo_total_cents,
           o.in_stock, o.source, o.price_scope, o.observed_at
    from observations o
    where o.chain_id in :chain_ids
      and o.product_id in :product_ids
      and o.store_id is null
      and o.status = 'accepted'
      and o.observed_at >= :since
    order by o.chain_id, o.observed_at desc, o.id desc
    """
).bindparams(bindparam("chain_ids", expanding=True), bindparam("product_ids", expanding=True))


_BULK_STORE_SQL = text(
    """
    select distinct on (o.store_id, o.product_id)
           o.store_id, o.product_id, o.price_cents, o.was_price_cents,
           o.unit_price_cents_per_l, o.promo_kind, o.promo_qty, o.promo_total_cents,
           o.in_stock, o.source, o.price_scope, o.observed_at
    from observations o
    where o.store_id in :store_ids
      and o.product_id in :product_ids
      and o.status = 'accepted'
      and o.observed_at >= :since
    order by o.store_id, o.product_id, o.observed_at desc, o.id desc
    """
).bindparams(
    bindparam("store_ids", expanding=True), bindparam("product_ids", expanding=True)
)

_BULK_CHAIN_SQL = text(
    """
    select distinct on (o.chain_id, o.product_id)
           o.chain_id, o.product_id, o.price_cents, o.was_price_cents,
           o.unit_price_cents_per_l, o.promo_kind, o.promo_qty, o.promo_total_cents,
           o.in_stock, o.source, o.price_scope, o.observed_at
    from observations o
    where o.chain_id in :chain_ids
      and o.product_id in :product_ids
      and o.store_id is null
      and o.status = 'accepted'
      and o.observed_at >= :since
    order by o.chain_id, o.product_id, o.observed_at desc, o.id desc
    """
).bindparams(
    bindparam("chain_ids", expanding=True), bindparam("product_ids", expanding=True)
)


def bulk_latest_prices(
    session: Session,
    stores: list[Store],
    product_ids: list[int],
    since: datetime,
) -> dict[tuple[int, int], dict]:
    """Latest accepted price for every (store, product) pair in two queries.

    Store-specific sightings win; chain-wide online prices are attributed to each
    store of that chain, same rule as latest_prices().
    """
    if not stores or not product_ids:
        return {}
    store_ids = [store.id for store in stores]
    chain_ids = sorted({store.chain_id for store in stores})
    rows = session.execute(
        _BULK_STORE_SQL,
        {"store_ids": store_ids, "product_ids": product_ids, "since": since},
    ).mappings()
    found: dict[tuple[int, int], dict] = {
        (row["store_id"], row["product_id"]): dict(row) for row in rows
    }
    chain_rows = session.execute(
        _BULK_CHAIN_SQL,
        {"chain_ids": chain_ids, "product_ids": product_ids, "since": since},
    ).mappings()
    chain_prices = {(row["chain_id"], row["product_id"]): dict(row) for row in chain_rows}
    for store in stores:
        for product_id in product_ids:
            key = (store.id, product_id)
            if key in found:
                continue
            row = chain_prices.get((store.chain_id, product_id))
            if row is not None:
                found[key] = {**row, "store_id": store.id, "price_scope": "chain"}
    return found


def latest_prices(
    session: Session,
    stores: list[Store],
    product_ids: list[int] | int,
    since: datetime,
) -> dict[int, Observation]:
    """Newest accepted price per store.

    Store-specific sightings (SpudShed, crowd) win; otherwise the chain-wide
    online price is attributed to every store in that chain.
    """
    if not stores:
        return {}
    if isinstance(product_ids, int):
        product_ids = [product_ids]
    store_ids = [store.id for store in stores]
    rows = session.execute(
        _STORE_SCOPE_SQL, {"store_ids": store_ids, "product_ids": product_ids, "since": since}
    ).mappings()
    by_store: dict[int, Observation] = {row["store_id"]: Observation(**dict(row)) for row in rows}

    missing_chains = {store.chain_id for store in stores if store.id not in by_store}
    if missing_chains:
        chain_rows = session.execute(
            _CHAIN_SCOPE_SQL,
            {"chain_ids": sorted(missing_chains), "product_ids": product_ids, "since": since},
        ).mappings()
        chain_prices = {row["chain_id"]: Observation(**dict(row)) for row in chain_rows}
        for store in stores:
            if store.id in by_store or store.chain_id not in chain_prices:
                continue
            chain_row = chain_prices[store.chain_id]
            by_store[store.id] = Observation(
                id=chain_row.id,
                store_id=store.id,
                chain_id=chain_row.chain_id,
                product_id=chain_row.product_id,
                price_cents=chain_row.price_cents,
                was_price_cents=chain_row.was_price_cents,
                unit_price_cents_per_l=chain_row.unit_price_cents_per_l,
                promo_kind=chain_row.promo_kind,
                promo_qty=chain_row.promo_qty,
                promo_total_cents=chain_row.promo_total_cents,
                in_stock=chain_row.in_stock,
                source=chain_row.source,
                price_scope="chain",
                observed_at=chain_row.observed_at,
            )
    return by_store


def product_group_ids(session: Session, product: Product) -> list[int]:
    """All product rows that represent the same drink (aliases included)."""
    root_id = product.canonical_product_id or product.id
    ids = {root_id, product.id}
    ids.update(
        session.scalars(
            select(Product.id).where(Product.canonical_product_id == root_id)
        )
    )
    return sorted(ids)


def get_product_by_slug(session: Session, slug: str) -> Product:
    product = session.scalar(select(Product).where(Product.slug == slug))
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown product slug: {slug}")
    return product


def get_product_by_barcode(session: Session, barcode: str) -> Product:
    product = session.scalar(select(Product).where(Product.barcode == barcode))
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown barcode: {barcode}")
    return product


def format_cents(cents: int | None) -> str:
    if cents is None:
        return ""
    return f"${cents / 100:.2f}"


def age_hours(observed_at: datetime, now: datetime | None = None) -> float:
    now = now or utcnow()
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return round((now - observed_at).total_seconds() / 3600, 1)


def default_since(max_age_hours: int) -> datetime:
    return utcnow() - timedelta(hours=max_age_hours)


def clamp_radius(radius_km: float | None) -> float:
    from ..config import settings

    if radius_km is None:
        return settings.default_radius_km
    return max(0.5, min(radius_km, settings.max_radius_km))
