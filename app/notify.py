"""Alert evaluation: turn a price change into a notification row.

Runs after each scrape and on a timer. Deliberately conservative: one
notification per alert per 24h, and only when the price is at or below the
threshold the user set.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .api.deps import bulk_latest_prices, format_cents, utcnow
from .models import Chain, Notification, PriceAlert, Product, Store

log = logging.getLogger(__name__)

QUIET_PERIOD = timedelta(hours=24)
FRESH_WINDOW = timedelta(hours=48)


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

        stmt = select(Store).where(Store.is_active.is_(True))
        if alert.store_id is not None:
            stmt = stmt.where(Store.id == alert.store_id)
        if alert.chain_code:
            stmt = stmt.join(Chain).where(Chain.code == alert.chain_code)
        stores = list(session.scalars(stmt.limit(400)))
        if not stores:
            continue

        prices = bulk_latest_prices(
            session, stores, [product.id for product in products], now - FRESH_WINDOW
        )
        best = None
        for (store_id, product_id), row in prices.items():
            if row["price_cents"] > alert.max_price_cents:
                continue
            if best is None or row["price_cents"] < best["price_cents"]:
                best = {
                    "price_cents": row["price_cents"],
                    "store": next((store for store in stores if store.id == store_id), None),
                    "product": next(
                        (product for product in products if product.id == product_id), None
                    ),
                    "promo_kind": row.get("promo_kind") or "none",
                    "price_scope": row.get("price_scope") or "store",
                }
        if best is None or best["store"] is None or best["product"] is None:
            continue

        title = f"{best['product'].brand} {best['product'].name} {format_cents(best['price_cents'])}"
        suburb = f", {best['store'].suburb}" if best["store"].suburb else ""
        scope_note = " (online price)" if best["price_scope"] == "chain" else ""
        body = f"At {best['store'].name}{suburb}{scope_note}"
        session.add(
            Notification(
                device_id=alert.device_id,
                kind="alert",
                title=title,
                body=body,
                payload={
                    "alert_id": alert.id,
                    "product_slug": best["product"].slug,
                    "store_id": best["store"].id,
                    "price_cents": best["price_cents"],
                    "promo_kind": best["promo_kind"],
                    "price_scope": best["price_scope"],
                },
            )
        )
        alert.last_notified_at = now
        created += 1

    session.commit()
    if created:
        log.info("queued %s price alert notification(s)", created)
    return created
