"""Crowd contribution API: device registration, price submissions, voting, reports."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..daily import record_daily_price
from ..db import get_session
from ..models import Chain, Device, Observation, ObservationVote, Product, Store
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
from .deps import find_product_by_barcode, format_cents, utcnow

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


# Two devices reporting the same price at the same shop within the hour are one
# fact, not two. Folding them keeps the extra reports useful as corroboration --
# the feed says "$3.50, 5 sightings" -- instead of letting five identical rows
# compete to be the store's "latest" price.
SIGHTING_FOLD_WINDOW = timedelta(hours=1)

# store, product, price: what a sighting actually asserts. The hour around it is
# what decides whether another row is the same assertion or a new one.
SightingKey = tuple[int | None, int, int]


def _sighting_key(observation: Observation) -> SightingKey:
    return (observation.store_id, observation.product_id, observation.price_cents)


def fold_crowd_sightings(
    observations: list[Observation],
) -> list[tuple[Observation, int]]:
    """Collapse repeat crowd reports of one price at one store inside an hour.

    Returns the surviving rows newest-first, each with the number of sightings it
    stands for. The newest row of a group is the representative because that is
    the one a client would show as "latest"; older rows inside the window are
    dropped from the response, not from the ledger -- they remain separate
    devices' reports, and their count is the corroboration this returns.

    Scrape rows never fold: a scraper writes one row per price change, so two
    scrape rows at the same price are two real observations, and a price change
    is exactly what the feed exists to show.
    """
    # All callers order newest-first; sort defensively so a caller that forgets
    # cannot fold an older report over a newer one.
    ordered = sorted(observations, key=lambda row: (row.observed_at, row.id or 0), reverse=True)
    kept: list[Observation] = []
    sightings: list[int] = []
    group_at: dict[SightingKey, int] = {}
    for row in ordered:
        if row.source == "crowd":
            position = group_at.get(_sighting_key(row))
            if position is not None:
                anchor = kept[position]
                if anchor.observed_at - row.observed_at < SIGHTING_FOLD_WINDOW:
                    sightings[position] += 1
                    continue
            group_at[_sighting_key(row)] = len(kept)
        kept.append(row)
        sightings.append(1)
    return list(zip(kept, sightings))


def _sightings_in_window(session: Session, observation: Observation) -> int:
    """How many crowd reports back this exact price at this store.

    The window ends at the report itself rather than at "now", so a receipt
    sighting submitted for an hour ago is folded with the reports it actually
    agrees with instead of with whatever happened since. Rejected rows do not
    count: they are reports we have already decided were wrong.
    """
    total = session.scalar(
        select(func.count())
        .select_from(Observation)
        .where(
            Observation.source == "crowd",
            Observation.status != "rejected",
            Observation.store_id == observation.store_id,
            Observation.product_id == observation.product_id,
            Observation.price_cents == observation.price_cents,
            Observation.observed_at > observation.observed_at - SIGHTING_FOLD_WINDOW,
            Observation.observed_at <= observation.observed_at,
        )
    )
    return int(total or 0)


def _serialize(session: Session, observations: list[Observation]) -> list[ObservationOut]:
    if not observations:
        return []
    rows = fold_crowd_sightings(observations)
    ids = [obs.id for obs, _ in rows]
    votes = {
        row.observation_id: (int(row.confirms or 0), abs(int(row.disputes or 0)))
        for row in session.execute(
            select(VOTE_COUNTS).where(VOTE_COUNTS.c.observation_id.in_(ids))
        )
    }
    out = []
    for obs, sightings in rows:
        item = ObservationOut.model_validate(obs)
        confirms, disputes = votes.get(obs.id, (0, 0))
        item.confirms = confirms
        item.disputes = disputes
        item.sightings = sightings
        out.append(item)
    return out


def _serialize_one(session: Session, observation: Observation) -> ObservationOut:
    """Serialize a single row, counting every report held in the ledger.

    A list response can only count the rows it was handed, but a write response
    knows the row it just stored, so it can afford the tally query and must not
    report a sighting as uncorroborated when four other devices said the same.
    """
    item = _serialize(session, [observation])[0]
    item.sightings = _sightings_in_window(session, observation)
    return item


# How a crowd price is judged before it is allowed to influence everyone else.
#
# These are triage thresholds, not a verdict: a flagged sighting is only held
# for two human votes, so a false positive costs a reviewer a glance while a
# false negative quietly corrupts a store's price. The cuts below are chosen to
# sit well outside normal retail variation but inside obvious data errors.
#
# * "Far out" is a modified z-score of 3.5. That is the conventional
#   Iglewicz-Hoaglin cut-off for small samples, and it stays meaningful when the
#   population is only a handful of rows (a plain sigma cut-off is not).
# * The scale floor matters more than the z: supermarket chains price a can at
#   one level right across a city, so the median absolute deviation is often
#   exactly 0 and the score would divide by zero. Treating 12% of the median as
#   the smallest believable spread means a fixed-price chain behaves like a
#   roughly 1.65x / 0.35x band around its own median -- tighter than the old
#   fixed 2.5x / 0.4x, and still loose enough to let half-price and clearance
#   sightings through.
# * Four comparable prices is the minimum that can be called a population;
#   below that we simply do not flag rather than guess.
OUTLIER_Z = 3.5
OUTLIER_MAD_FLOOR = 0.12
REFERENCE_MIN_SAMPLES = 4
# Enough rows for a stable median, few enough that prices from a year of promos
# do not drag the middle away from what the chain charges now.
REFERENCE_SAMPLE_LIMIT = 30
REFERENCE_WINDOW = timedelta(days=180)


@dataclass(frozen=True)
class _Reference:
    """The population a submitted price is judged against, in cents."""

    median_cents: float
    scale_cents: float
    samples: int

    def is_outlier(self, price_cents: int) -> bool:
        # 0.6745 turns the MAD into a standard-deviation equivalent for a normal
        # distribution, which is what gives OUTLIER_Z its usual meaning.
        return 0.6745 * abs(price_cents - self.median_cents) / self.scale_cents > OUTLIER_Z


def _recent_prices(
    session: Session,
    product_id: int,
    chain_filter: ColumnElement[bool],
    device_id: int | None,
    since: datetime,
) -> list[int]:
    """Recent accepted prices for one product from one comparable population."""
    stmt = (
        select(Observation.price_cents)
        .where(
            Observation.product_id == product_id,
            Observation.status == "accepted",
            Observation.observed_at >= since,
            chain_filter,
        )
        .order_by(Observation.observed_at.desc(), Observation.id.desc())
        .limit(REFERENCE_SAMPLE_LIMIT)
    )
    if device_id is not None:
        # A reporter must not set their own yardstick: without this a device can
        # walk a bad series up (or down) and have every step look normal, and one
        # device's repeated typo becomes the reference for everyone else.
        stmt = stmt.where(Observation.device_id.is_distinct_from(device_id))
    return list(session.scalars(stmt))


def _reference_price(
    session: Session, product: Product, store: Store, device_id: int | None
) -> _Reference | None:
    """Build the population this sighting should be judged against.

    Prices are not comparable across retail formats: a 7-Eleven can is not
    "expensive" beside a Coles can, it is a different market. So the reference is
    the same chain, widened to chains of the same kind (supermarket / convenience
    / servo) only when this chain has too little history of its own. Both price
    scopes stay in the population because a chain-wide online price is still that
    chain's price for the product; it is the *store* attribution that differs.
    """
    since = utcnow() - REFERENCE_WINDOW
    prices = _recent_prices(
        session, product.id, Observation.chain_id == store.chain_id, device_id, since
    )
    if len(prices) < REFERENCE_MIN_SAMPLES:
        same_kind_chains = select(Chain.id).where(Chain.kind == store.chain.kind)
        prices = _recent_prices(
            session, product.id, Observation.chain_id.in_(same_kind_chains), device_id, since
        )
    if len(prices) < REFERENCE_MIN_SAMPLES:
        return None
    median = float(statistics.median(prices))
    mad = float(statistics.median([abs(price - median) for price in prices]))
    return _Reference(
        median_cents=median,
        scale_cents=max(mad, OUTLIER_MAD_FLOOR * median),
        samples=len(prices),
    )


@router.post("/observations", response_model=ObservationOut, status_code=status.HTTP_201_CREATED)
def submit_observation(
    payload: ObservationIn,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> ObservationOut:
    device = current_device(authorization, session=session)
    write_limiter.check(f"obs:{device.id}")
    return _record_observation(payload, device, session)


def _record_observation(payload: ObservationIn, device: Device, session: Session) -> ObservationOut:
    """Validate and store one sighting for an already-charged caller.

    The hour's quota is charged by the HTTP handlers, not here: a batch is one
    act of reporting and has to be admitted in one decision, while a single
    sighting costs one slot. Everything else -- validation, outlier triage,
    idempotency -- stays per item, so one bad line fails only that line.
    """
    store = session.get(Store, payload.store_id)
    if store is None or not store.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown store")

    product: Product | None = None
    if payload.product_slug:
        product = session.scalar(select(Product).where(Product.slug == payload.product_slug))
    elif payload.barcode:
        product = find_product_by_barcode(session, payload.barcode)
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
        return _serialize_one(session, recent)

    reference = _reference_price(session, product, store, device.id)
    needs_review = reference is not None and reference.is_outlier(payload.price_cents)

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
    if observation.status == "accepted":
        # A crowd price is a sighting at one shop, so it belongs in the daily
        # ledger under that shop - otherwise today's history would only ever
        # reflect scrapes, and crowd prices would never move a deal badge.
        record_daily_price(
            session,
            product_id=product.id,
            chain_id=store.chain_id,
            store_id=store.id,
            price_cents=payload.price_cents,
            was_price_cents=payload.was_price_cents,
            promo_kind=payload.promo_kind,
            source="crowd",
            observed_at=observed_at,
        )
    session.commit()
    return _serialize_one(session, observation)


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
    return _serialize_one(session, observation)


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
    """Same validation as /v1/observations, one row per item, per-item errors.

    The hourly quota is charged once for the whole docket, sized to its line
    count. Charging per line made a receipt spend the device's entire hour: a
    50-line docket took 83% of it, the next receipt 429'd line by line, and the
    user was left with a half-submitted docket they could not finish.
    """
    device = current_device(authorization, session=session)
    write_limiter.charge(f"obs:{device.id}", n=len(payload.items))
    results: list[BatchObservationResult] = []
    for index, item in enumerate(payload.items):
        try:
            saved = _record_observation(item, device, session)
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
