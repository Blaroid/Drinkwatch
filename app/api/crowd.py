"""Crowd contribution API: device registration, price submissions, voting, reports."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import Device, Observation, ObservationVote, Product, Store
from ..schemas import (
    BatchObservationIn,
    BatchObservationResult,
    DeviceMeOut,
    DeviceOut,
    DeviceRegisterIn,
    ObservationIn,
    ObservationOut,
    ReceiptCandidate,
    ReceiptParseIn,
    ReportIn,
    VoteIn,
)
from ..security import current_device, hash_token, new_device_token, read_limiter, write_limiter
from .deps import format_cents, utcnow

router = APIRouter(prefix="/v1", tags=["crowd"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/devices", response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
def register_device(
    payload: DeviceRegisterIn,
    request: Request,
    session: Session = Depends(get_session),
) -> DeviceOut:
    read_limiter.check("register:" + _client_ip(request))
    token = new_device_token()
    device = Device(
        token_hash=hash_token(token),
        display_name=payload.display_name,
        platform=payload.platform,
        app_version=payload.app_version,
        last_seen_at=utcnow(),
    )
    session.add(device)
    session.commit()
    return DeviceOut(
        id=device.id,
        token=token,
        display_name=device.display_name,
        reputation=device.reputation,
        created_at=device.created_at,
    )


@router.get("/me", response_model=DeviceMeOut)
def me(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> DeviceMeOut:
    device = current_device(authorization, session=session)
    session.commit()
    return DeviceMeOut.model_validate(device)


VOTE_COUNTS = (
    select(
        ObservationVote.observation_id.label("observation_id"),
        func.sum(func.greatest(ObservationVote.vote, 0)).label("confirms"),
        func.sum(func.least(ObservationVote.vote, 0)).label("disputes"),
    )
    .group_by(ObservationVote.observation_id)
    .subquery()
)


def _serialize(session: Session, observations: list[Observation]) -> list[ObservationOut]:
    if not observations:
        return []
    ids = [obs.id for obs in observations]
    counts = {
        row.observation_id: (int(row.confirms or 0), abs(int(row.disputes or 0)))
        for row in session.execute(
            select(VOTE_COUNTS).where(VOTE_COUNTS.c.observation_id.in_(ids))
        )
    }
    out = []
    for obs in observations:
        item = ObservationOut.model_validate(obs)
        confirms, disputes = counts.get(obs.id, (0, 0))
        item.confirms = confirms
        item.disputes = disputes
        out.append(item)
    return out


def _reference_price(session: Session, product: Product) -> int | None:
    """Median of the last few accepted prices for this product, any store."""
    rows = list(
        session.scalars(
            select(Observation.price_cents)
            .where(Observation.product_id == product.id, Observation.status == "accepted")
            .order_by(Observation.observed_at.desc())
            .limit(9)
        )
    )
    if not rows:
        return None
    rows.sort()
    return rows[len(rows) // 2]


@router.post("/observations", response_model=ObservationOut, status_code=status.HTTP_201_CREATED)
def submit_observation(
    payload: ObservationIn,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> ObservationOut:
    device = current_device(authorization, session=session)
    write_limiter.check(f"obs:{device.id}")

    store = session.get(Store, payload.store_id)
    if store is None or not store.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown store")

    product: Product | None = None
    if payload.product_slug:
        product = session.scalar(select(Product).where(Product.slug == payload.product_slug))
    elif payload.barcode:
        product = session.scalar(select(Product).where(Product.barcode == payload.barcode))
    if product is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "supply a known product_slug or barcode (search /v1/products first)",
        )

    if payload.price_cents > settings.max_price_cents:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "price above allowed maximum")
    if payload.promo_kind == "multibuy" and not payload.promo_qty:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "multibuy observations need promo_qty"
        )

    observed_at = payload.observed_at or utcnow()
    now = utcnow()
    if observed_at.tzinfo is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "observed_at must include a timezone")
    if observed_at > now + timedelta(minutes=10):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "observed_at is in the future")
    if observed_at < now - timedelta(days=30):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "observed_at is too old (30 days max)")

    # Same device, same product, same store, same price within 12h: idempotent.
    recent = session.scalar(
        select(Observation)
        .where(
            Observation.device_id == device.id,
            Observation.store_id == store.id,
            Observation.product_id == product.id,
            Observation.price_cents == payload.price_cents,
            Observation.observed_at >= now - timedelta(hours=12),
        )
        .order_by(Observation.observed_at.desc())
    )
    if recent is not None:
        return _serialize(session, [recent])[0]

    reference = _reference_price(session, product)
    needs_review = False
    if reference:
        ratio = payload.price_cents / reference
        needs_review = ratio > 2.5 or ratio < 0.4

    unit_per_litre = None
    if product.size_ml and product.pack_count:
        litres = product.size_ml * product.pack_count / 1000
        if litres > 0:
            unit_per_litre = round(payload.price_cents / litres)

    photo_id = None
    if payload.photo_token:
        from ..models import Photo

        photo = session.scalar(select(Photo).where(Photo.token == payload.photo_token))
        if photo is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown photo token")
        photo_id = photo.id

    observation = Observation(
        store_id=store.id,
        chain_id=store.chain_id,
        product_id=product.id,
        price_cents=payload.price_cents,
        was_price_cents=payload.was_price_cents,
        unit_price_cents_per_l=unit_per_litre,
        promo_kind=payload.promo_kind,
        promo_qty=payload.promo_qty,
        promo_total_cents=payload.promo_total_cents,
        in_stock=payload.in_stock,
        source="crowd",
        price_scope="store",
        device_id=device.id,
        note=payload.note,
        photo_url=payload.photo_url,
        photo_id=photo_id,
        observed_at=observed_at,
        status="needs_review" if needs_review else "accepted",
    )
    session.add(observation)
    device.reputation = (device.reputation or 0) + (0 if needs_review else 1)
    session.commit()
    return _serialize(session, [observation])[0]


@router.post("/observations/{observation_id}/vote", response_model=ObservationOut)
def vote(
    observation_id: int,
    payload: VoteIn,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> ObservationOut:
    device = current_device(authorization, session=session)
    write_limiter.check(f"vote:{device.id}")
    observation = session.get(Observation, observation_id)
    if observation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown observation")
    if observation.device_id == device.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot vote on your own observation")

    existing = session.get(ObservationVote, (observation.id, device.id))
    if existing is None:
        session.add(
            ObservationVote(observation_id=observation.id, device_id=device.id, vote=payload.vote)
        )
    else:
        existing.vote = payload.vote
        existing.created_at = utcnow()

    confirms, disputes = session.execute(
        select(func.count())
        .select_from(ObservationVote)
        .where(ObservationVote.observation_id == observation.id, ObservationVote.vote == 1)
    ).one()[0], session.execute(
        select(func.count())
        .select_from(ObservationVote)
        .where(ObservationVote.observation_id == observation.id, ObservationVote.vote == -1)
    ).one()[0]

    if observation.status == "needs_review" and int(confirms or 0) >= 2:
        observation.status = "accepted"
    if int(disputes or 0) >= 3 and observation.status == "accepted":
        observation.status = "needs_review"
    session.commit()
    return _serialize(session, [observation])[0]


@router.post("/reports", status_code=status.HTTP_201_CREATED)
def report(
    payload: ReportIn,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> dict:
    from ..models import Report

    device = current_device(authorization, session=session)
    read_limiter.check(f"report:{device.id}")
    if payload.observation_id is not None and session.get(Observation, payload.observation_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown observation")
    entry = Report(
        observation_id=payload.observation_id,
        device_id=device.id,
        reason=payload.reason,
        note=payload.note,
    )
    session.add(entry)
    session.commit()
    return {"id": entry.id, "status": "received"}


@router.get("/me/observations", response_model=list[ObservationOut])
def my_observations(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[ObservationOut]:
    device = current_device(authorization, session=session)
    session.commit()
    rows = list(
        session.scalars(
            select(Observation)
            .where(Observation.device_id == device.id)
            .order_by(Observation.observed_at.desc())
            .limit(limit)
        )
    )
    return _serialize(session, rows)


@router.post("/observations/batch", response_model=list[BatchObservationResult], summary="Submit many sightings at once (receipt flow)")
def submit_batch(
    payload: BatchObservationIn,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> list[BatchObservationResult]:
    """Same validation as /v1/observations, one row per item, per-item errors."""
    results: list[BatchObservationResult] = []
    for index, item in enumerate(payload.items):
        try:
            saved = submit_observation(item, authorization=authorization, session=session)
            results.append(
                BatchObservationResult(index=index, status=saved.status, observation_id=saved.id)
            )
        except HTTPException as exc:
            session.rollback()
            results.append(
                BatchObservationResult(index=index, status="error", error=str(exc.detail))
            )
    return results


@router.post("/receipts/parse", response_model=list[ReceiptCandidate], summary="Match OCR receipt lines to products")
def parse_receipt(
    payload: ReceiptParseIn,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> list[ReceiptCandidate]:
    """The app does the OCR (ML Kit); this matches the text lines to our catalogue.

    Returns the best product guess per line so the user only has to confirm.
    """
    from ..scrape.normalize import flavour_tokens
    from sqlalchemy import select as _select

    device = current_device(authorization, session=session)
    read_limiter.check(f"receipt:{device.id}")
    session.commit()

    products = list(
        session.scalars(_select(Product).where(Product.status != "rejected", Product.is_active.is_(True)))
    )
    index = [
        (product, flavour_tokens(f"{product.brand} {product.name} {product.variant or ''}"))
        for product in products
    ]

    price_pattern = re.compile(r"\$?\s*(\d{1,3}\.\d{2})\s*$")
    candidates: list[ReceiptCandidate] = []
    for raw_line in payload.text.splitlines()[:120]:
        line = raw_line.strip()
        if not line:
            continue
        match = price_pattern.search(line)
        price_cents = None
        name_part = line
        if match:
            price_cents = round(float(match.group(1)) * 100)
            name_part = line[: match.start()].strip()
        if len(name_part) < 3:
            continue
        wanted = flavour_tokens(name_part)
        best_product = None
        best_score = 0.0
        for product, tokens in index:
            if not tokens or not wanted:
                continue
            score = len(wanted & tokens) / len(wanted | tokens)
            if score > best_score:
                best_score = score
                best_product = product
        candidates.append(
            ReceiptCandidate(
                line=line,
                price_cents=price_cents,
                product_slug=best_product.slug if best_product and best_score >= 0.34 else None,
                label=(
                    f"{best_product.brand} {best_product.name}"
                    if best_product and best_score >= 0.34
                    else None
                ),
                score=round(best_score, 2),
            )
        )
    return candidates
