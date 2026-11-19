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

from .api.deps import bulk_latest_prices, format_cents, haversine_km, nearby_stores, utcnow
from .models import Chain, Notification, PriceAlert, Product, Store, Watchlist

log = logging.getLogger(__name__)

QUIET_PERIOD = timedelta(hours=24)
FRESH_WINDOW = timedelta(hours=48)
MEDIAN_WINDOW_DAYS = 30
# Ceiling on stores examined by a *radius* alert per run, applied after the
# exact haversine sort so the nearest stores survive. A radius is the only case
# that can return more than the state holds, and an alert without one must see
# every active store: 544 rows is not a scanning problem, and quietly dropping
# the tail made ~26% of the state unable to fire (audit #34 item 5).
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
    alert: PriceAlert,
    price_cents: int,
    median_cents: float | None,
    promo_kind: str = "none",
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
    # "Tell me when a special starts": the price does not have to beat any
    # threshold, the promotion itself is the news.
    if alert.on_special and promo_kind not in ("none", ""):
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
        # No radius means "every active store", as API.md promises. Order by id
        # only so a run is reproducible; there is no cap to select against.
        return list(session.scalars(stmt.order_by(Store.id)))

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


# One statement both claims the alert and re-checks the quiet period. Two
# evaluations that read the same old last_notified_at cannot both queue a
# notification: the second blocks on the row lock until the first commits, then
# re-evaluates the WHERE against the committed value and claims nothing. Compare
# with an advisory lock, which would also need a transaction held across the
# whole scan; the claim only serialises the alert that is actually about to fire.
_CLAIM_SQL = text(
    """
    update price_alerts
       set last_notified_at = :now
     where id = :alert_id
       and (last_notified_at is null or last_notified_at < :cutoff)
    returning id
    """
)


def _claim_alert(session: Session, alert_id: int, now: datetime) -> bool:
    """Mark the alert as notified now, if no one else has inside the quiet period.

    Returns False when another evaluation already claimed it, which is the
    signal to skip queueing. The in-memory PriceAlert is deliberately left alone:
    the committed row is the authority and the caller does not read the
    attribute again.
    """
    claimed = session.execute(
        _CLAIM_SQL,
        {"now": now, "cutoff": now - QUIET_PERIOD, "alert_id": alert_id},
    ).scalar_one_or_none()
    return claimed is not None


def evaluate_alerts(session: Session, limit: int = 500) -> int:
    """Queue at most one notification per alert, even if runs overlap.

    The timer, the post-scrape call and the admin endpoint can all be inside
    this function at once. The quiet-period check below is only a fast path over
    rows this session loaded; the per-alert claim before the insert is what makes
    double-notification impossible.
    """
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
            matched, drop = _matches(
                alert,
                row["price_cents"],
                median_cents,
                row.get("promo_kind") or "none",
            )
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

        # Claim before queueing. A failed claim means a concurrent run already
        # queued this alert, so adding our copy would be the duplicate.
        if not _claim_alert(session, alert.id, now):
            continue

        session.add(
            Notification(
                device_id=alert.device_id,
                kind="alert",
                title=title,
                body=body,
                payload=payload,
            )
        )
        # Commit per alert: it releases the claimed row immediately for the next
        # run instead of holding every claim until the whole batch is done.
        session.commit()
        created += 1

    session.commit()
    if created:
        log.info("queued %s price alert notification(s)", created)
    return created


DIGEST_MIN_GAP = timedelta(days=6)
DIGEST_MAX_LINES = 4


def weekly_digest(session: Session, limit: int = 500) -> int:
    """One "what this week looks like" notification per watchlist.

    Runs on a Sunday evening timer. It reuses the same price read as an alert, but
    the question is different: not "did something cross a threshold" but "where
    should I buy my usual cans this week". Devices that already have a digest from
    the last six days are skipped, so a rerun cannot double-post.
    """
    watches = list(session.scalars(select(Watchlist).limit(limit)))
    if not watches:
        return 0

    now = utcnow()
    created = 0
    for watch in watches:
        slugs = [slug for slug in (watch.slugs or []) if slug]
        if not slugs:
            continue
        recent = session.scalar(
            select(Notification.id)
            .where(
                Notification.device_id == watch.device_id,
                Notification.kind == "digest",
                Notification.created_at >= now - DIGEST_MIN_GAP,
            )
            .limit(1)
        )
        if recent is not None:
            continue

        products = list(
            session.scalars(
                select(Product).where(Product.slug.in_(slugs), Product.status != "rejected")
            )
        )
        if not products:
            continue

        lat = watch.lat if watch.lat is not None else -31.9523
        lon = watch.lon if watch.lon is not None else 115.8613
        stores = [
            store
            for store, _ in nearby_stores(session, lat, lon, watch.radius_km or 15, limit=STORE_SCAN_LIMIT)
        ]
        if not stores:
            continue
        prices = bulk_latest_prices(session, stores, [p.id for p in products], now - FRESH_WINDOW)
        store_by_id = {store.id: store for store in stores}
        product_by_id = {product.id: product for product in products}

        cheapest: dict[int, dict[str, Any]] = {}
        for (store_id, product_id), row in prices.items():
            store = store_by_id.get(store_id)
            product = product_by_id.get(product_id)
            if store is None or product is None:
                continue
            best = cheapest.get(product_id)
            if best is None or row["price_cents"] < best["price_cents"]:
                cheapest[product_id] = {
                    "price_cents": row["price_cents"],
                    "store": store,
                    "product": product,
                    "promo_kind": row.get("promo_kind") or "none",
                }
        if not cheapest:
            continue

        lines = []
        for entry in sorted(cheapest.values(), key=lambda e: e["price_cents"])[:DIGEST_MAX_LINES]:
            product = entry["product"]
            label = " ".join(part for part in (product.brand, product.name) if part)
            if product.size_ml:
                label += f" {product.size_ml}ml"
                if product.pack_count > 1:
                    label += f" x{product.pack_count}"
            suburb = f" {entry['store'].suburb}" if entry["store"].suburb else ""
            promo = " (special)" if entry["promo_kind"] not in ("none", "") else ""
            lines.append(f"{label} {format_cents(entry['price_cents'])} at {entry['store'].name}{suburb}{promo}")

        session.add(
            Notification(
                device_id=watch.device_id,
                kind="digest",
                title="Your cans this week",
                body="; ".join(lines),
                payload={
                    "products": [entry["product"].slug for entry in cheapest.values()],
                    "best_total_cents": sum(entry["price_cents"] for entry in cheapest.values()),
                },
            )
        )
        created += 1

    session.commit()
    if created:
        log.info("queued %s weekly digest(s)", created)
    return created
