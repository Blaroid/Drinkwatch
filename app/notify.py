"""Alert evaluation: turn a price change into a notification row.

Runs after each scrape and on a timer. Deliberately conservative: one
notification per alert per 24h, and only when the price is at or below the
threshold the user set, or has fallen far enough below its own recent median.

Two optional filters narrow which stores an alert may look at: a radius around
the device's location (so "under $2.50" stops pinging about a servo 60 km away)
and the older explicit store/chain pin. Both are off by default, which is the
behaviour every alert had before they existed.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from .api.deps import bulk_latest_prices, format_cents, haversine_km, utcnow
from .models import Chain, Notification, PriceAlert, Product, Store

log = logging.getLogger(__name__)

QUIET_PERIOD = timedelta(hours=24)
FRESH_WINDOW = timedelta(hours=48)
MEDIAN_WINDOW_DAYS = 30
# Cap on stores examined per alert per run. The timer runs this often, so an
# unbounded scan of every store in the state is not worth it; a radius narrows
# the list, it never raises this ceiling.
STORE_SCAN_LIMIT = 400
LOCAL_TZ = ZoneInfo("Australia/Perth")


def _median_window(now: datetime) -> tuple[date, date]:
    """Start (inclusive) and end (exclusive) of the median baseline, in Perth days.

    price_daily rows are keyed by local date (see 006_price_daily.sql), so the
    window has to be cut on the same days. The current day is excluded: today's
    price is what the drop is measured *against*, and folding it into the median
    would damp the very signal the trigger is looking for.
    """
    today = now.astimezone(LOCAL_TZ).date()
    return today - timedelta(days=MEDIAN_WINDOW_DAYS), today


_MEDIAN_SQL = text(
    """
    select chain_id, product_id,
           percentile_cont(0.5) within group (order by price_cents) as median_cents
      from price_daily
     where chain_id in :chain_ids
       and product_id in :product_ids
       and day >= :since_day
       and day < :until_day
     group by chain_id, product_id
    """
).bindparams(bindparam("chain_ids", expanding=True), bindparam("product_ids", expanding=True))


def _median_prices(
    session: Session,
    chain_ids: list[int],
    product_ids: list[int],
    now: datetime,
) -> dict[tuple[int, int], float]:
    """Median price per (chain, product) over the last 30 days.

    Chain-level rather than store-level: price_daily only keeps one row per
    day/chain/product, and observation dedupe leaves a single store with too
    few rows to take a median of. A chain's own median is also the "usual
    price" a shopper compares against, which is what the trigger promises.
    """
    if not chain_ids or not product_ids:
        return {}
    since_day, until_day = _median_window(now)
    rows = session.execute(
        _MEDIAN_SQL,
        {
            "chain_ids": chain_ids,
            "product_ids": product_ids,
            "since_day": since_day,
            "until_day": until_day,
        },
    ).mappings()
    return {
        (row["chain_id"], row["product_id"]): float(row["median_cents"])
        for row in rows
        if row["median_cents"] is not None
    }


def _drop_percent(price_cents: int, median_cents: float | None) -> float | None:
    """How far below its median a price sits, or None when it is not below it."""
    if median_cents is None or median_cents <= 0 or price_cents >= median_cents:
        return None
    return (median_cents - price_cents) / median_cents * 100.0


def _matches(
    alert: PriceAlert, price_cents: int, median_cents: float | None
) -> tuple[bool, float | None]:
    """Does this price satisfy the alert, and by how much of a drop?

    Either trigger is enough: the absolute ceiling (`max_price_cents`), which is
    the original behaviour, or a fall of at least `min_drop_percent` below the
    30-day median. The relative one exists because "tell me about any real drop"
    is what people actually want; a fixed dollar threshold has to be re-guessed
    every time the normal price moves.

    The returned drop is None when the price is not below its median, or when
    the alert has no relative trigger and no median was fetched.
    """
    drop = _drop_percent(price_cents, median_cents)
    if alert.max_price_cents is not None and price_cents <= alert.max_price_cents:
        return True, drop
    if alert.min_drop_percent is not None and drop is not None and drop >= alert.min_drop_percent:
        return True, drop
    return False, None


def _in_radius(alert: PriceAlert, store: Store) -> bool:
    """True when the alert has no radius, or this store sits inside it."""
    if alert.lat is None or alert.lon is None or alert.radius_km is None:
        return True
    if store.lat is None or store.lon is None:
        # A store without coordinates cannot be shown to be near the device, so
        # keeping it would reintroduce exactly the far-away ping the radius is
        # meant to stop.
        return False
    return haversine_km(alert.lat, alert.lon, store.lat, store.lon) <= alert.radius_km


def _alert_stores(session: Session, alert: PriceAlert) -> list[Store]:
    """Active stores this alert is allowed to consider, honouring its filters."""
    stmt = select(Store).where(Store.is_active.is_(True))
    if alert.store_id is not None:
        stmt = stmt.where(Store.id == alert.store_id)
    if alert.chain_code:
        stmt = stmt.join(Chain).where(Chain.code == alert.chain_code)
    if alert.lat is None or alert.lon is None or alert.radius_km is None:
        return list(session.scalars(stmt.limit(STORE_SCAN_LIMIT)))

    # Cheap bounding box in SQL, exact haversine on the rows that come back: one
    # indexed range scan beats pulling every active store into Python to measure it.
    dlat = alert.radius_km / 111.0
    dlon = alert.radius_km / max(0.01, 111.0 * math.cos(math.radians(alert.lat)))
    stmt = stmt.where(
        Store.lat.is_not(None),
        Store.lon.is_not(None),
        Store.lat.between(alert.lat - dlat, alert.lat + dlat),
        Store.lon.between(alert.lon - dlon, alert.lon + dlon),
    )
    nearby = [store for store in session.scalars(stmt) if _in_radius(alert, store)]
    # Nearest first: if the scan cap ever bites, keep the shops the user is most
    # likely to drive to rather than an arbitrary subset.
    nearby.sort(
        key=lambda store: haversine_km(alert.lat, alert.lon, store.lat, store.lon)  # type: ignore[arg-type]
    )
    return nearby[:STORE_SCAN_LIMIT]


def evaluate_alerts(session: Session, limit: int = 500) -> int:
    alerts = list(
        session.scalars(
            select(PriceAlert).where(PriceAlert.is_active.is_(True)).order_by(PriceAlert.id).limit(limit)
        )
    )
    if not alerts:
        return 0

    now = utcnow()
    created = 0
    for alert in alerts:
        if alert.last_notified_at and now - alert.last_notified_at < QUIET_PERIOD:
            continue

        products: list[Product] = []
        if alert.product_id is not None:
            product = session.get(Product, alert.product_id)
            if product is not None:
                products = [product]
        elif alert.family_slug:
            products = list(
                session.scalars(
                    select(Product).where(
                        Product.family_slug == alert.family_slug, Product.status != "rejected"
                    )
                )
            )
        if not products:
            continue

        stores = _alert_stores(session, alert)
        if not stores:
            continue

        store_by_id = {store.id: store for store in stores}
        product_by_id = {product.id: product for product in products}
        prices = bulk_latest_prices(
            session, stores, [product.id for product in products], now - FRESH_WINDOW
        )
        # Only pay for the median query when a relative trigger can use it.
        medians: dict[tuple[int, int], float] = {}
        if alert.min_drop_percent is not None:
            medians = _median_prices(
                session,
                sorted({store.chain_id for store in stores}),
                [product.id for product in products],
                now,
            )

        best: dict[str, Any] | None = None
        for (store_id, product_id), row in prices.items():
            store = store_by_id.get(store_id)
            product = product_by_id.get(product_id)
            if store is None or product is None:
                continue
            median_cents = medians.get((store.chain_id, product_id))
            matched, drop = _matches(alert, row["price_cents"], median_cents)
            if not matched:
                continue
            if best is not None and row["price_cents"] >= best["price_cents"]:
                continue
            best = {
                "price_cents": row["price_cents"],
                "store": store,
                "product": product,
                "promo_kind": row.get("promo_kind") or "none",
                "price_scope": row.get("price_scope") or "store",
                "median_cents": median_cents,
                "drop_percent": drop,
            }
        if best is None:
            continue

        title = f"{best['product'].brand} {best['product'].name} {format_cents(best['price_cents'])}"
        suburb = f", {best['store'].suburb}" if best["store"].suburb else ""
        scope_note = " (online price)" if best["price_scope"] == "chain" else ""
        body = f"At {best['store'].name}{suburb}{scope_note}"
        payload = {
            "alert_id": alert.id,
            "product_slug": best["product"].slug,
            "store_id": best["store"].id,
            "price_cents": best["price_cents"],
            "promo_kind": best["promo_kind"],
            "price_scope": best["price_scope"],
        }
        # Say what the price fell from whenever we measured a drop, and only then
        # add the keys: an alert with no relative trigger keeps its old payload
        # shape, which older app builds already parse.
        if best["drop_percent"] is not None:
            median_cents = int(round(best["median_cents"]))
            body += (
                f" - down {best['drop_percent']:.0f}% from its 30-day median"
                f" {format_cents(median_cents)}"
            )
            payload["median_cents"] = median_cents
            payload["drop_percent"] = round(best["drop_percent"], 1)

        session.add(
            Notification(
                device_id=alert.device_id,
                kind="alert",
                title=title,
                body=body,
                payload=payload,
            )
        )
        alert.last_notified_at = now
        created += 1

    session.commit()
    if created:
        log.info("queued %s price alert notification(s)", created)
    return created
