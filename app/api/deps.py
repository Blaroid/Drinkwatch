"""Shared query helpers for the HTTP layer."""

from __future__ import annotations

import math
from dataclasses import dataclass
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
           o.id, o.store_id, o.chain_id, o.product_id, o.price_cents, o.was_price_cents,
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
           o.id, o.store_id, o.chain_id, o.product_id, o.price_cents, o.was_price_cents,
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


def _observation_dict(row: Observation) -> dict:
    """The plain-dict view of a row that bulk_latest_prices() returns.

    pricing.collect_offers() and notify.evaluate_alerts() index these dicts, so
    the shape is public API even though it is only built here. `id`/`chain_id`
    are deliberately not included: they were never part of this shape and no
    caller wants them.
    """
    return {
        "store_id": row.store_id,
        "product_id": row.product_id,
        "price_cents": row.price_cents,
        "was_price_cents": row.was_price_cents,
        "unit_price_cents_per_l": row.unit_price_cents_per_l,
        "promo_kind": row.promo_kind,
        "promo_qty": row.promo_qty,
        "promo_total_cents": row.promo_total_cents,
        "in_stock": row.in_stock,
        "source": row.source,
        "price_scope": row.price_scope,
        "observed_at": row.observed_at,
    }


def bulk_latest_prices(
    session: Session,
    stores: list[Store],
    product_ids: list[int],
    since: datetime,
) -> dict[tuple[int, int], dict]:
    """Freshest accepted price for every (store, product) pair in two queries.

    Both candidate rows are always fetched — the store's own sightings and the
    chain-wide online price attributed to every store in that chain — and the
    more recently observed one wins, via the same `_freshest()` rule
    `latest_prices()` uses. A stale store row therefore cannot shadow last
    night's scrape for the planner (pricing.collect_offers) or an alert
    (notify.evaluate_alerts). Ties go to the store-scoped row.

    Values keep the historical dict shape rather than a StorePrice pair: the
    losing row has no consumer on this path, so only the winner is returned.
    """
    if not stores or not product_ids:
        return {}
    store_ids = [store.id for store in stores]
    chain_ids = sorted({store.chain_id for store in stores})
    store_rows = {
        (row["store_id"], row["product_id"]): Observation(**dict(row))
        for row in session.execute(
            _BULK_STORE_SQL,
            {"store_ids": store_ids, "product_ids": product_ids, "since": since},
        ).mappings()
    }
    chain_rows = {
        (row["chain_id"], row["product_id"]): Observation(**dict(row))
        for row in session.execute(
            _BULK_CHAIN_SQL,
            {"chain_ids": chain_ids, "product_ids": product_ids, "since": since},
        ).mappings()
    }
    found: dict[tuple[int, int], dict] = {}
    for store in stores:
        for product_id in product_ids:
            chosen = _freshest(
                store_rows.get((store.id, product_id)),
                chain_rows.get((store.chain_id, product_id)),
                store.id,
            )
            if chosen is not None:
                found[(store.id, product_id)] = _observation_dict(chosen.primary)
    return found


@dataclass(frozen=True)
class StorePrice:
    """The freshest row for one store, plus the row it beat.

    `primary` is what the store is showing now; `fallback` is the other scope's
    row, kept because it is still evidence — usually the only first-hand
    sighting for that shelf — and dropping it here would force a caller that
    wants the other view to query the ledger again.
    """

    primary: Observation
    fallback: Observation | None = None


def _as_utc(moment: datetime) -> datetime:
    # Compare in one timezone: a naive timestamp (age_hours() tolerates those
    # as well) must not raise TypeError halfway through a request.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _chain_attributed(row: Observation, store_id: int) -> Observation:
    """Copy a chain-wide row (store_id NULL) so it can stand for one store."""
    return Observation(
        id=row.id,
        store_id=store_id,
        chain_id=row.chain_id,
        product_id=row.product_id,
        price_cents=row.price_cents,
        was_price_cents=row.was_price_cents,
        unit_price_cents_per_l=row.unit_price_cents_per_l,
        promo_kind=row.promo_kind,
        promo_qty=row.promo_qty,
        promo_total_cents=row.promo_total_cents,
        in_stock=row.in_stock,
        source=row.source,
        price_scope="chain",
        observed_at=row.observed_at,
    )


def _freshest(
    store_row: Observation | None,
    chain_row: Observation | None,
    store_id: int,
) -> StorePrice | None:
    """Pick whichever of the two candidate rows was observed more recently.

    Freshest wins rather than "store scope wins" because age is what makes a
    price wrong: a crowd report from 71 hours ago silently shadowing last
    night's scrape is how a stale $3.15 ends up the headline "cheapest" beside
    a real $3.50. On an exact tie the store-scoped row wins, since it was seen
    on that shelf rather than published for the whole chain.
    """
    if chain_row is None:
        return StorePrice(primary=store_row) if store_row is not None else None
    chain_offer = _chain_attributed(chain_row, store_id)
    if store_row is None:
        return StorePrice(primary=chain_offer)
    if _as_utc(chain_row.observed_at) > _as_utc(store_row.observed_at):
        return StorePrice(primary=chain_offer, fallback=store_row)
    return StorePrice(primary=store_row, fallback=chain_offer)


def latest_prices(
    session: Session,
    stores: list[Store],
    product_ids: list[int] | int,
    since: datetime,
) -> dict[int, StorePrice]:
    """Freshest accepted price per store, whichever scope it came from.

    Both candidates are always fetched: the store's own sightings and the
    chain-wide online price attributed to every store in that chain. Whichever
    was observed most recently becomes the offer, so a stale store row can no
    longer shadow last night's scrape; the losing row is kept as a fallback.
    """
    if not stores:
        return {}
    if isinstance(product_ids, int):
        product_ids = [product_ids]
    store_ids = [store.id for store in stores]
    rows = session.execute(
        _STORE_SCOPE_SQL, {"store_ids": store_ids, "product_ids": product_ids, "since": since}
    ).mappings()
    store_rows = {row["store_id"]: Observation(**dict(row)) for row in rows}

    chain_rows = {
        row["chain_id"]: Observation(**dict(row))
        for row in session.execute(
            _CHAIN_SCOPE_SQL,
            {
                "chain_ids": sorted({store.chain_id for store in stores}),
                "product_ids": product_ids,
                "since": since,
            },
        ).mappings()
    }

    by_store: dict[int, StorePrice] = {}
    for store in stores:
        chosen = _freshest(store_rows.get(store.id), chain_rows.get(store.chain_id), store.id)
        if chosen is not None:
            by_store[store.id] = chosen
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
