"""Public read API: what the Android app and widget call."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import and_, bindparam, case, func, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import ColumnElement
from sqlalchemy.sql.selectable import Subquery

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


def _offer(
    product: Product,
    store: Store,
    distance: float | None,
    obs: Observation,
    now: datetime,
) -> OfferOut:
    """Render one observation as the public offer row.

    Shared by the single-product path and the batched suggestion path so both
    produce byte-identical rows.
    """
    per_can = round(obs.price_cents / product.pack_count) if product.pack_count > 1 else None
    per_litre = obs.unit_price_cents_per_l
    if per_litre is None and product.size_ml and product.pack_count:
        litres = product.size_ml * product.pack_count / 1000
        per_litre = round(obs.price_cents / litres) if litres > 0 else None
    return OfferOut(
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
        store_pair = by_id.get(store_id)
        if store_pair is None:
            continue
        store, distance = store_pair
        # #4 gives every store the fresher of its own sighting and the chain
        # price; #14 renders it through one shared helper.
        offers.append(_offer(product, store, distance, current.primary, now))
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


# The observation columns a batched price lookup needs to rebuild a row; the
# same set latest_prices() hands back to _offer().
_PRICE_COLUMNS = (
    Observation.id,
    Observation.store_id,
    Observation.chain_id,
    Observation.product_id,
    Observation.price_cents,
    Observation.was_price_cents,
    Observation.unit_price_cents_per_l,
    Observation.promo_kind,
    Observation.promo_qty,
    Observation.promo_total_cents,
    Observation.in_stock,
    Observation.source,
    Observation.price_scope,
    Observation.observed_at,
)


def _latest_statement(keys: list[ColumnElement], conditions: list[ColumnElement]) -> Subquery:
    """Accepted prices ranked per ``(product_id, *keys)``; row 1 is the freshest.

    A window function is the portable spelling of the ``distinct on`` the price
    helpers use, so the same statement also runs on SQLite in tests. Ordering is
    the one ``latest_prices()`` applies: newest sighting, then highest id.
    """
    return (
        select(
            *_PRICE_COLUMNS,
            func.row_number()
            .over(
                partition_by=[Observation.product_id, *keys],
                order_by=(Observation.observed_at.desc(), Observation.id.desc()),
            )
            .label("rn"),
        )
        .where(*conditions)
        .subquery()
    )


def _latest_rows(
    session: Session, keys: list[ColumnElement], conditions: list[ColumnElement]
) -> list[dict]:
    """Freshest accepted observation per ``(product_id, *keys)``, in one query."""
    ranked = _latest_statement(keys, conditions)
    rows = session.execute(select(ranked).where(ranked.c.rn == 1)).mappings()
    return [dict(row) for row in rows]


def _row_observation(row: dict) -> Observation:
    """Rebuild a detached Observation from a batched row (drops the rank column)."""
    return Observation(**{key: value for key, value in row.items() if key != "rn"})


def _row_time(row: dict) -> datetime:
    """A batched row's timestamp in UTC, because fixtures hand us naive rows."""
    moment: datetime = row["observed_at"]
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _suggest_offers(
    session: Session,
    products: list[Product],
    store_distances: list[tuple[Store, float]],
    since: datetime,
) -> dict[int, OfferOut]:
    """Cheapest offer for each product, in a fixed number of queries.

    The suggestion list only needs one offer per candidate, but
    ``_offers_for_product()`` costs roughly four round trips - two of them just
    to resolve the alias group - which was being paid for each of up to 60
    candidates. Here the whole candidate set shares one alias lookup and one
    price query per scope, then reduces with the same ``(price, distance)``
    rule and tie-break as ``_offers_for_product()[0]``.

    Deal stats are deliberately not fetched: the widget never serialises
    ``OfferOut.stats``, so the 90-day percentile query would be pure overhead.
    """
    if not products or not store_distances:
        return {}
    stores = [store for store, _ in store_distances]
    by_id = {store.id: (store, distance) for store, distance in store_distances}

    # Same alias groups product_group_ids() builds, once for the whole set.
    roots = {product.canonical_product_id or product.id for product in products}
    members: dict[int, list[int]] = {root: [root] for root in roots}
    for product_id, root_id in session.execute(
        select(Product.id, Product.canonical_product_id).where(
            Product.canonical_product_id.in_(roots)
        )
    ):
        members[root_id].append(product_id)
    product_ids = sorted({product_id for ids in members.values() for product_id in ids})
    roots_of: dict[int, list[int]] = {}
    for root, ids in members.items():
        for product_id in ids:
            roots_of.setdefault(product_id, []).append(root)

    store_rows = _latest_rows(
        session,
        [Observation.store_id],
        [
            Observation.store_id.in_([store.id for store in stores]),
            Observation.product_id.in_(product_ids),
            Observation.status == "accepted",
            Observation.observed_at >= since,
        ],
    )
    chain_rows = _latest_rows(
        session,
        [Observation.chain_id],
        [
            Observation.store_id.is_(None),
            Observation.chain_id.in_({store.chain_id for store in stores}),
            Observation.product_id.in_(product_ids),
            Observation.status == "accepted",
            Observation.observed_at >= since,
        ],
    )

    def freshest_per_root(rows: list[dict], key: str) -> dict[int, dict[int, dict]]:
        """Collapse alias siblings: per root and scope key, keep the newest row."""
        best: dict[int, dict[int, dict]] = {}
        for row in rows:
            for root in roots_of[row["product_id"]]:
                by_key = best.setdefault(root, {})
                current = by_key.get(row[key])
                if current is None or (row["observed_at"], row["id"]) > (
                    current["observed_at"],
                    current["id"],
                ):
                    by_key[row[key]] = row
        return best

    in_store = freshest_per_root(store_rows, "store_id")
    in_chain = freshest_per_root(chain_rows, "chain_id")

    now = utcnow()
    best_by_product: dict[int, OfferOut] = {}
    for product in products:
        root = product.canonical_product_id or product.id
        observations: dict[int, Observation] = {}
        # Per store, whichever scope was observed more recently wins - the rule
        # latest_prices() applies through deps._freshest(). Preferring the store
        # row unconditionally let a three-day-old crowd sighting shadow last
        # night's chain-wide scrape, so a suggestion could quote a different
        # price for the same drink than the tracked row above it. Ties go to the
        # store row, seen on that shelf rather than published for the chain.
        own_by_store = in_store.get(root, {})
        chain_by_chain = in_chain.get(root, {})
        for store in stores:
            own = own_by_store.get(store.id)
            chain = chain_by_chain.get(store.chain_id)
            if chain is not None and (own is None or _row_time(chain) > _row_time(own)):
                observations[store.id] = _row_observation(
                    {**chain, "store_id": store.id, "price_scope": "chain"}
                )
            elif own is not None:
                observations[store.id] = _row_observation(own)

        winner: tuple[Store, float, Observation] | None = None
        for store_id, obs in observations.items():
            store_pair = by_id.get(store_id)
            if store_pair is None:
                continue
            store, distance = store_pair
            if winner is None or (obs.price_cents, distance or 999) < (
                winner[2].price_cents,
                winner[1] or 999,
            ):
                winner = (store, distance, obs)
        if winner is not None:
            best_by_product[product.id] = _offer(product, winner[0], winner[1], winner[2], now)
    return best_by_product


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


# The widget is polled on every phone unlock, so let the phone's HTTP cache
# serve it and revalidate rather than cross the tunnel each time. Five minutes
# is short enough that a price change still shows up while someone is shopping.
_WIDGET_MAX_AGE_SECONDS = 300
# Bump when the validator's inputs change, so a validator handed out by an older
# build cannot be replayed against the new one.
_WIDGET_ETAG_VERSION = "w1"


def _freshest(current: datetime | None, candidate: datetime) -> datetime:
    """Keep the newest observation timestamp seen while building the payload."""
    if candidate.tzinfo is None:  # fixtures and backfills can hand us naive rows
        candidate = candidate.replace(tzinfo=timezone.utc)
    if current is None or candidate > current:
        return candidate
    return current


def _widget_etag(query: str, freshest: datetime | None, items: list[WidgetItemOut]) -> str:
    """Weak validator for /v1/widget, derived from the data rather than the bytes.

    The payload cannot be hashed directly: generated_at and every item's
    age_hours move on each request, so a digest of the response would never
    match and the widget would never earn a 304. Hash what actually decides the
    rows instead - the freshest observation behind them, plus every row field
    except age_hours as a backstop for edits (a renamed product, a corrected
    distance) that do not advance that timestamp. The query string keeps one
    widget URL's validator from being replayed against another.
    """
    digest = hashlib.sha256()
    digest.update(_WIDGET_ETAG_VERSION.encode())
    digest.update(b"\x00")
    digest.update(query.encode())
    digest.update(b"\x00")
    digest.update(freshest.isoformat().encode() if freshest is not None else b"-")
    for item in items:
        digest.update(b"\x00")
        digest.update(item.model_dump_json(exclude={"age_hours"}).encode())
    # Weak: the prices are unchanged, but age_hours in the body keeps drifting.
    return f'W/"{digest.hexdigest()[:32]}"'


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 weak comparison of an If-None-Match list against our validator."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    target = etag.removeprefix("W/")
    return any(
        candidate.strip().removeprefix("W/") == target for candidate in if_none_match.split(",")
    )


@router.get("/widget", response_model=WidgetOut, summary="Smallest possible payload for a phone widget")
def widget(
    response: Response,
    request: Request,
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    products: str | None = Query(default=None, description="comma separated slugs (max 8)"),
    radius_km: float | None = Query(default=None, gt=0),
    max_age_hours: int | None = Query(default=None, ge=1, le=720),
    suggest: int = Query(default=0, ge=0, le=8, description="append this many similar drinks"),
    if_none_match: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> WidgetOut | Response:
    # Part of the validator: the same prices at a different radius/location is a
    # different widget, and a shared cache should not answer one with the other.
    request_query = request.url.query
    radius = clamp_radius(radius_km)
    since = default_since(max_age_hours or settings.default_max_age_hours)

    if products:
        slugs = [s.strip() for s in products.split(",") if s.strip()][:8]
        catalogue = [get_product_by_slug(session, slug) for slug in slugs]
    else:
        # No explicit list: the energy drinks on the most shelves. Count distinct
        # stores, not sightings — a chain scraped four times a day would otherwise
        # make seven shops look like twenty-eight and bury servo-only drinks.
        # Chain-wide online rows (Woolworths/Coles) have no store_id, so count
        # each such chain once; otherwise the two biggest chains score zero and
        # fall out of the default list even though they stock the drink.
        chain_wide = case((Observation.store_id.is_(None), Observation.chain_id))
        stocked_stores = (
            func.count(func.distinct(Observation.store_id))
            + func.count(func.distinct(chain_wide))
        )
        popularity = (
            select(Observation.product_id, stocked_stores.label("stores"))
            .where(Observation.observed_at >= since)
            .group_by(Observation.product_id)
            .subquery()
        )
        ranked = list(
            session.scalars(
                select(Product)
                .join(popularity, popularity.c.product_id == Product.id)
                .where(Product.is_active.is_(True), Product.category == "energy")
                .order_by(popularity.c.stores.desc(), Product.brand, Product.name)
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
    freshest: datetime | None = None
    for found in catalogue:
        offers = _offers_for_product(session, found, stores, since)
        if not offers:
            continue
        best = offers[0]
        freshest = _freshest(freshest, best.observed_at)
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
        best_offers = _suggest_offers(session, candidates, stores, since)
        for candidate in candidates:
            best = best_offers.get(candidate.id)
            if best is None:
                continue
            same_brand = 0 if candidate.brand.lower() in wanted_brands else 1
            ranked.append((same_brand, best.price_cents, candidate, best))
        ranked.sort(key=lambda row: (row[0], row[1]))
        for _, _, candidate, best in ranked[:suggest]:
            freshest = _freshest(freshest, best.observed_at)
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

    cache_headers = {
        "Cache-Control": f"public, max-age={_WIDGET_MAX_AGE_SECONDS}",
        "ETag": _widget_etag(request_query, freshest, items),
    }
    if _etag_matches(if_none_match, cache_headers["ETag"]):
        # The client's copy is current; 304 keeps the tunnel bytes to headers only.
        return Response(status_code=304, headers=cache_headers)
    response.headers.update(cache_headers)
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
