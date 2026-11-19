"""Price alerts, in-app notifications and the caffeine log.

Notifications are stored server-side and fetched by the app (GET /v1/notifications).
Push delivery (FCM) needs your Firebase project credentials; the table and payload
shape are already what an FCM message would carry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import CaffeineEntry, Notification, PriceAlert, Product, Store
from ..schemas import (
    AlertIn,
    AlertOut,
    CaffeineEntryOut,
    CaffeineIn,
    CaffeineTodayOut,
    NotificationOut,
)
from ..scrape.normalize import caffeine_mg_for
from ..security import current_device, read_limiter, write_limiter
from .deps import utcnow

router = APIRouter(prefix="/v1", tags=["alerts"])

LOCAL_TZ = ZoneInfo("Australia/Perth")


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> list[AlertOut]:
    device = current_device(authorization, session=session)
    session.commit()
    return [
        AlertOut.model_validate(alert)
        for alert in session.scalars(
            select(PriceAlert).where(PriceAlert.device_id == device.id).order_by(PriceAlert.id.desc())
        )
    ]


@router.post("/alerts", response_model=AlertOut, status_code=status.HTTP_201_CREATED)
def create_alert(
    payload: AlertIn,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> AlertOut:
    device = current_device(authorization, session=session)
    write_limiter.check(f"alert:{device.id}")

    if not payload.product_slug and not payload.family_slug:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "give a product_slug or a family_slug"
        )
    if (
        payload.max_price_cents is None
        and payload.min_drop_percent is None
        and not payload.on_special
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "give a max_price_cents, a min_drop_percent, or on_special",
        )
    location_fields = (payload.lat, payload.lon, payload.radius_km)
    if sum(field is None for field in location_fields) not in (0, len(location_fields)):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "give lat, lon and radius_km together, or none of them",
        )
    if payload.radius_km is not None and payload.radius_km > settings.max_radius_km:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"radius_km must be at most {settings.max_radius_km:g} km",
        )

    product = None
    if payload.product_slug:
        product = session.scalar(select(Product).where(Product.slug == payload.product_slug))
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown product")
    if payload.store_id is not None and session.get(Store, payload.store_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown store")

    existing = session.scalar(
        select(PriceAlert).where(
            PriceAlert.device_id == device.id,
            PriceAlert.max_price_cents == payload.max_price_cents,
            PriceAlert.min_drop_percent == payload.min_drop_percent,
            PriceAlert.on_special == payload.on_special,
            PriceAlert.product_id == (product.id if product else None),
            PriceAlert.family_slug == (payload.family_slug or (product.family_slug if product else None)),
            PriceAlert.chain_code == payload.chain_code,
            PriceAlert.store_id == payload.store_id,
            PriceAlert.lat == payload.lat,
            PriceAlert.lon == payload.lon,
            PriceAlert.radius_km == payload.radius_km,
            PriceAlert.is_active.is_(True),
        )
    )
    if existing is not None:
        return AlertOut.model_validate(existing)

    alert = PriceAlert(
        device_id=device.id,
        product_id=product.id if product else None,
        family_slug=payload.family_slug or (product.family_slug if product else None),
        chain_code=payload.chain_code,
        store_id=payload.store_id,
        max_price_cents=payload.max_price_cents,
        min_drop_percent=payload.min_drop_percent,
        on_special=payload.on_special,
        lat=payload.lat,
        lon=payload.lon,
        radius_km=payload.radius_km,
    )
    session.add(alert)
    session.commit()
    return AlertOut.model_validate(alert)


@router.delete("/alerts/{alert_id}", response_model=AlertOut)
def delete_alert(
    alert_id: int,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> AlertOut:
    device = current_device(authorization, session=session)
    alert = session.get(PriceAlert, alert_id)
    if alert is None or alert.device_id != device.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown alert")
    alert.is_active = False
    session.commit()
    return AlertOut.model_validate(alert)


@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> list[NotificationOut]:
    device = current_device(authorization, session=session)
    session.commit()
    stmt = select(Notification).where(Notification.device_id == device.id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc()).limit(limit)
    return [NotificationOut.model_validate(row) for row in session.scalars(stmt)]


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: int,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> NotificationOut:
    device = current_device(authorization, session=session)
    row = session.get(Notification, notification_id)
    if row is None or row.device_id != device.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown notification")
    row.read_at = utcnow()
    session.commit()
    return NotificationOut.model_validate(row)


@router.post("/caffeine", response_model=CaffeineEntryOut, status_code=status.HTTP_201_CREATED)
def log_caffeine(
    payload: CaffeineIn,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> CaffeineEntryOut:
    device = current_device(authorization, session=session)
    write_limiter.check(f"caffeine:{device.id}")

    product = None
    if payload.product_slug:
        product = session.scalar(select(Product).where(Product.slug == payload.product_slug))
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown product")

    if product is not None:
        per_unit = caffeine_mg_for(product)
        mg = int(round(per_unit * payload.qty))
    elif payload.mg_caffeine is not None:
        mg = payload.mg_caffeine
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "supply product_slug, or mg_caffeine with a label",
        )

    entry = CaffeineEntry(
        device_id=device.id,
        product_id=product.id if product else None,
        label=payload.label or (product.name if product else None),
        qty=payload.qty,
        mg_caffeine=mg,
        consumed_at=payload.consumed_at or utcnow(),
    )
    session.add(entry)
    session.commit()
    return CaffeineEntryOut.model_validate(entry)


@router.get("/caffeine/today", response_model=CaffeineTodayOut)
def caffeine_today(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> CaffeineTodayOut:
    device = current_device(authorization, session=session)
    session.commit()
    now_local = datetime.now(LOCAL_TZ)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    entries = list(
        session.scalars(
            select(CaffeineEntry)
            .where(
                CaffeineEntry.device_id == device.id,
                CaffeineEntry.consumed_at >= day_start.astimezone(timezone.utc),
            )
            .order_by(CaffeineEntry.consumed_at)
        )
    )
    total = int(sum(entry.mg_caffeine for entry in entries))
    return CaffeineTodayOut(
        date=day_start.date().isoformat(),
        total_mg=total,
        limit_mg=settings.daily_caffeine_limit_mg,
        remaining_mg=max(0, settings.daily_caffeine_limit_mg - total),
        entries=[CaffeineEntryOut.model_validate(entry) for entry in entries],
    )
