"""Public read API: what the Android app and widget call."""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, bindparam, func, or_, select, text
from sqlalchemy.orm import Session

from .. import __version__
from ..config import settings
from ..db import get_session
from ..models import Chain, Observation, Product, Store
from ..schemas import (
    ChainOut,
    DealStatsOut,
    LeaderboardEntry,
    MetaOut,
    OfferOut,
    ProductOut,
    ProductPricesOut,
    StoreOut,
    WidgetItemOut,
    WidgetOut,
)
from .crowd import fold_crowd_sightings
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

_DEAL_STATS_SQL = text(
    """
    select chain_id,
           store_id,
           percentile_cont(0.5) within group (order by price_cents) as median_cents,
           min(price_cents) as min_cents,
           count(*) as samples
      from price_daily
     where product_id in :product_ids
       and day >= current_date - interval '90 days'
     group by chain_id, store_id
    """
).bindparams(bindparam("product_ids", expanding=True))

# Below this many daily samples a scope's history is a couple of odd days, not a
# price level, so the badge stays unset rather than guessing.
_MIN_DEAL_SAMPLES = 3


def _deal_stats(session: Session, product_ids: list[int]) -> dict[tuple[int, int | None], dict]:
    """90-day history per (chain, store); a None store is the chain-level row."""
    return {
        (row["chain_id"], row["store_id"]): dict(row)
        for row in session.execute(_DEAL_STATS_SQL, {"product_ids": product_ids}).mappings()
    }


def _deal_row(
    stats: dict[tuple[int, int | None], dict], chain_id: int, store_id: int | None
) -> dict | None:
    """The history an offer should be judged against.

    An offer is one shop's price, so it is judged against that shop's own 90
    days. Chains that publish one price state-wide (Woolworths, Coles) write no
    store rows, and a store with too little history of its own, so both fall
    back to the chain-level row the writers keep aggregated.
    """
    own = stats.get((chain_id, store_id))
    if own and (own.get("samples") or 0) >= _MIN_DEAL_SAMPLES:
        return own
    chain_row = stats.get((chain_id, None))
    if chain_row and (chain_row.get("samples") or 0) >= _MIN_DEAL_SAMPLES:
        return chain_row
    return None


def _classify_deal(price_cents: int, median_cents: int | None, min_cents: int | None) -> str:
    if not median_cents or median_cents <= 0:
        return "unknown"
    if min_cents and price_cents <= round(min_cents * 1.05):
        return "great"
    if price_cents <= round(median_cents * 0.95):
        return "good"
    if price_cents <= round(median_cents * 1.1):
        return "typical"
    return "high"


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
    for store_id, current in observations.items():
        store, distance = by_id.get(store_id, (None, None))
        if store is None:
            continue
        # `latest_prices` already chose the fresher of the store sighting and
        # the chain-wide online price, so a stale row cannot set this offer.
        obs = current.primary
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

    group_ids = product_group_ids(session, product)
    stats = _deal_stats(session, group_ids)
    for offer in offers:
        store_pair = by_id.get(offer.store_id)
        if store_pair is None:
            continue
        row = _deal_row(stats, store_pair[0].chain_id, offer.store_id)
        if row is None:
            continue
        median = round(row["median_cents"]) if row.get("median_cents") else None
        minimum = row.get("min_cents")
        offer.stats = DealStatsOut(
            median_90d_cents=median,
            min_90d_cents=minimum,
            deal=_classify_deal(offer.price_cents, median, minimum),
        )
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
    order = [Product.brand, Product.name, Product.pack_count]
    if q and q.strip():
        term = q.strip().lower()
        # "energy drink" is part of every energy product's meaning even though the
        # word is stripped from the name, so typing it still has to work.
        blob = func.lower(
            Product.brand
            + " "
            + Product.name
            + " "
            + func.coalesce(Product.variant, "")
            + " energy drink "
            + func.coalesce(func.cast(Product.size_ml, __import__("sqlalchemy").String), "")
            + " "
            + func.coalesce(func.cast(Product.size_ml, __import__("sqlalchemy").String), "")
            + "ml"
        )
        conditions = []
        for token in [part for part in term.split() if part]:
            # word-boundary hit (so "v" finds V, not Violet) or a fuzzy near-miss
            pattern = "\\m" + re.escape(token) + "\\M"
            conditions.append(
                or_(
                    blob.op("~")(pattern),
                    func.word_similarity(token, blob) > 0.40,
                )
            )
        stmt = stmt.where(and_(*conditions))
        order = [
            func.greatest(
                func.similarity(blob, term), func.word_similarity(term, blob)
            ).desc(),
            Product.brand,
            Product.name,
        ]
    if brand:
        stmt = stmt.where(func.lower(Product.brand) == brand.lower())
    if category:
        stmt = stmt.where(Product.category == category)
    stmt = stmt.order_by(*order).offset(offset).limit(limit)
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
    suggest: int = Query(default=0, ge=0, le=8, description="append this many similar drinks"),
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
        ranked = list(
            session.scalars(
                select(Product)
                .join(popularity, popularity.c.product_id == Product.id)
                .where(Product.is_active.is_(True), Product.category == "energy")
                .order_by(popularity.c.hits.desc(), Product.brand, Product.name)
                .limit(60)
            )
        )
        # Spread the default rows across brands instead of 8 Monster flavours.
        catalogue = []
        seen: dict[str, int] = {}
        for candidate in ranked:
            if seen.get(candidate.brand, 0) >= 2:
                continue
            seen[candidate.brand] = seen.get(candidate.brand, 0) + 1
            catalogue.append(candidate)
            if len(catalogue) == 8:
                break

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

    # "that's the end of your list" suggestions: same brand first, then whatever
    # else nearby has fresh prices.
    if suggest > 0 and items:
        wanted = {product.id for product in catalogue}
        wanted_brands = {product.brand.lower() for product in catalogue}
        candidates = list(
            session.scalars(
                select(Product)
                .where(
                    Product.is_active.is_(True),
                    Product.category == "energy",
                    Product.status != "rejected",
                    ~Product.id.in_(wanted),
                )
                .order_by(Product.brand, Product.name)
                .limit(60)
            )
        )
        ranked = []
        for candidate in candidates:
            offers = _offers_for_product(session, candidate, stores, since)
            if not offers:
                continue
            same_brand = 0 if candidate.brand.lower() in wanted_brands else 1
            ranked.append((same_brand, offers[0].price_cents, candidate, offers[0]))
        ranked.sort(key=lambda row: (row[0], row[1]))
        for _, _, candidate, best in ranked[:suggest]:
            items.append(
                WidgetItemOut(
                    slug=candidate.slug,
                    label=_label(candidate),
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
                    suggested=True,
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
    # Repeat crowd reports of one price are one sighting with a count, not five
    # rows in a row: this is the list the app renders as "recent sightings".
    # Folding after the SQL limit means a page can show fewer than `limit` rows.
    folded = fold_crowd_sightings(rows)
    store_ids = {r.store_id for r, _ in folded if r.store_id is not None}
    product_ids = {r.product_id for r, _ in folded}
    stores = {s.id: s for s in session.scalars(select(Store).where(Store.id.in_(store_ids)))}
    prods = {p.id: p for p in session.scalars(select(Product).where(Product.id.in_(product_ids)))}
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
            "sightings": sightings,
        }
        for row, sightings in folded
    ]


@router.get("/stores/search", response_model=list[StoreOut], summary="Find a store by name, suburb or postcode")
def stores_search(
    q: str = Query(min_length=1, max_length=60),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lon: float | None = Query(default=None, ge=-180, le=180),
    radius_km: float | None = Query(default=None, gt=0),
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[StoreOut]:
    """Text search over store name, suburb, postcode and chain name.

    When lat/lon are supplied the results are distance-annotated; a name that
    starts with the query outranks a substring hit, which then outranks a chain
    name hit (so "spud" finds the SpudSheds before every Woolworths).
    """
    needle = f"%{q.strip().lower()}%"
    prefix = f"{q.strip().lower()}%"
    rows = list(
        session.scalars(
            select(Store)
            .join(Chain, Chain.id == Store.chain_id)
            .where(
                Store.is_active.is_(True),
                func.lower(Store.name).like(needle)
                | func.lower(func.coalesce(Store.suburb, "")).like(needle)
                | func.coalesce(Store.postcode, "").like(needle)
                | func.lower(Chain.name).like(needle),
            )
            .limit(300)
        )
    )

    def rank(store: Store) -> tuple:
        name = (store.name or "").lower()
        suburb = (store.suburb or "").lower()
        if name.startswith(prefix) or suburb.startswith(prefix):
            bucket = 0
        elif q.strip().lower() in name or q.strip().lower() in suburb:
            bucket = 1
        else:
            bucket = 2
        return bucket

    ranked = sorted(rows, key=rank)
    if lat is not None and lon is not None:
        from .deps import haversine_km

        radius = clamp_radius(radius_km) if radius_km else None
        annotated = []
        for store in ranked:
            if store.lat is None or store.lon is None:
                continue
            distance = haversine_km(lat, lon, store.lat, store.lon)
            if radius is not None and distance > radius:
                continue
            annotated.append((store, round(distance, 2)))
        annotated.sort(key=lambda pair: (rank(pair[0]), pair[1]))
        annotated = annotated[:limit]
        result = []
        for store, distance in annotated:
            item = StoreOut.model_validate(store)
            item.distance_km = distance
            result.append(item)
        return result

    return [StoreOut.model_validate(store) for store in ranked[:limit]]


@router.get("/leaderboard", response_model=list[LeaderboardEntry], summary="Top crowd contributors")
def leaderboard(
    period: str = Query(default="week", pattern="^(week|month|all)$"),
    limit: int = Query(default=10, ge=1, le=50),
    session: Session = Depends(get_session),
) -> list[LeaderboardEntry]:
    from datetime import timedelta

    from ..models import Device

    stmt = (
        select(Observation.device_id, func.count().label("accepted"))
        .where(
            Observation.source == "crowd",
            Observation.status == "accepted",
            Observation.device_id.is_not(None),
        )
        .group_by(Observation.device_id)
        .order_by(func.count().desc())
        .limit(limit)
    )
    if period != "all":
        days = 7 if period == "week" else 30
        stmt = stmt.where(Observation.observed_at >= utcnow() - timedelta(days=days))

    entries: list[LeaderboardEntry] = []
    for rank, row in enumerate(session.execute(stmt).all(), start=1):
        device = session.get(Device, row[0])
        label = (device.display_name if device else None) or f"anon-{str(row[0]).zfill(3)}"
        entries.append(
            LeaderboardEntry(
                rank=rank,
                device=label,
                reputation=device.reputation if device else 0,
                accepted=int(row[1]),
            )
        )
    return entries
