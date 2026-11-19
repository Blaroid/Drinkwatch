"""Barcode lookup and crowd-sourced product onboarding.

Order of resolution: our own catalogue (products.normalised_barcode and
listings.barcode), then Open Food Facts as a suggestion only. Australian energy
drinks are almost absent from OFF (measured: 7 of 8 real AU barcodes 404), so an
unknown barcode goes to the pending-product flow, where the user supplies the
name printed on the can and an admin confirms it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..caffeine import caffeine_from_off, plausible_caffeine
from ..config import settings
from ..db import get_session
from ..models import BarcodeCache, Device, Observation, Store
from ..schemas import BarcodeLookupOut, PendingProductIn, PendingProductOut, ProductOut
from ..scrape.normalize import (
    barcode_variants,
    ean13_checksum_ok,
    match_product,
    normalize_barcode,
    parse_pack,
    register_barcode,
)
from ..security import current_device, read_limiter, write_limiter
from .deps import find_product_by_barcode

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["products"])

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
OFF_FIELDS = "product_name,brands,quantity,caffeine_100g,image_front_url"
OFF_TTL = timedelta(days=30)


def _cached(session: Session, barcode: str) -> dict | None:
    row = session.scalar(select(BarcodeCache).where(BarcodeCache.barcode == barcode))
    if row is None:
        return None
    return row.payload


def _cache(session: Session, barcode: str, payload: dict) -> None:
    row = session.scalar(select(BarcodeCache).where(BarcodeCache.barcode == barcode))
    if row is None:
        row = BarcodeCache(barcode=barcode, source=str(payload.get("source", "unknown")), payload=payload)
        session.add(row)
    else:
        row.payload = payload
        row.created_at = datetime.now(timezone.utc)
    session.commit()


def _cached_caffeine(session: Session, barcode: str) -> float | None:
    """Label caffeine from the Open Food Facts payload already cached.

    Deliberately no network call: the barcode GET normally populated this a
    moment earlier, and the pending-product write should not depend on a third
    party being up. The payload also keeps the raw ``caffeine_100g`` it came
    from, so the mg/100 mL figure can be recomputed if the conversion changes.
    """
    payload = _cached(session, barcode)
    if not payload or not payload.get("found"):
        return None
    return plausible_caffeine(payload.get("caffeine_mg_per_100ml"))


def external_lookup(session: Session, barcode: str) -> dict | None:
    """Open Food Facts fallback, cached (including negative results)."""
    if not settings.off_lookup_enabled:
        return None
    cached = _cached(session, barcode)
    if cached is not None:
        return cached or None
    try:
        with httpx.Client(timeout=6.0, headers={"User-Agent": settings.scrape_user_agent}) as client:
            response = client.get(OFF_URL.format(barcode=barcode), params={"fields": OFF_FIELDS})
    except Exception as exc:  # pragma: no cover - network path
        log.info("openfoodfacts lookup failed for %s: %s", barcode, exc)
        return None

    if response.status_code != 200:
        _cache(session, barcode, {"source": "openfoodfacts", "found": False})
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    product = payload.get("product") or {}
    if not product:
        _cache(session, barcode, {"source": "openfoodfacts", "found": False})
        return None
    caffeine = product.get("caffeine_100g")
    suggestion = {
        "source": "openfoodfacts",
        "found": True,
        "name": product.get("product_name"),
        "brand": product.get("brands"),
        "quantity": product.get("quantity"),
        "image_url": product.get("image_front_url"),
        "caffeine_100g": caffeine,
        "caffeine_mg_per_100ml": caffeine_from_off(caffeine),
    }
    _cache(session, barcode, suggestion)
    return suggestion


@router.get("/barcode/{barcode}", response_model=BarcodeLookupOut, summary="Resolve a scanned barcode")
def lookup(barcode: str, session: Session = Depends(get_session)) -> BarcodeLookupOut:
    normalised = normalize_barcode(barcode)
    # A GTIN-14's check digit covers its packaging indicator, so validate every form.
    checksum_ok = any(ean13_checksum_ok(variant) for variant in barcode_variants(barcode))
    product = find_product_by_barcode(session, barcode, normalised)
    if product is not None:
        return BarcodeLookupOut(
            found=True,
            barcode=barcode,
            normalised=normalised,
            checksum_ok=checksum_ok,
            product=ProductOut.model_validate(product),
        )

    suggestion = external_lookup(session, normalised or barcode) if normalised else None
    return BarcodeLookupOut(
        found=False,
        barcode=barcode,
        normalised=normalised,
        checksum_ok=checksum_ok,
        suggestion=suggestion,
        pending_hint=(
            "No match. Ask the user for the name and size on the can, then POST /v1/products/pending"
        ),
    )


@router.post(
    "/products/pending",
    response_model=PendingProductOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add an unknown can to the catalogue (queued for review)",
)
def submit_pending(
    payload: PendingProductIn,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> PendingProductOut:
    device: Device = current_device(authorization, session=session)
    write_limiter.check(f"pending:{device.id}")

    normalised = normalize_barcode(payload.barcode)
    if normalised is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "barcode not usable")

    store = None
    if payload.store_id is not None:
        store = session.get(Store, payload.store_id)
        if store is None or not store.is_active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown store")

    created = False
    product = find_product_by_barcode(session, payload.barcode, normalised)

    if product is None:
        size_ml = payload.size_ml
        pack_count = payload.pack_count
        if size_ml is None:
            size_ml, parsed_pack = parse_pack(payload.name)
            pack_count = pack_count if payload.pack_count > 1 else parsed_pack
        product, created = match_product(
            session,
            name=payload.name,
            brand=payload.brand,
            size_ml=size_ml,
            pack_count=pack_count,
            barcode=payload.barcode,
            category=payload.category,
            create=True,
        )
        if product is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "could not derive a product; include brand and size_ml",
            )
        if created:
            product.source = "crowd"
            product.status = "pending"

    # Index every code form against the product (and fill the primary column).
    register_barcode(session, product.id, payload.barcode, source="crowd")
    if normalised and not product.normalised_barcode:
        product.normalised_barcode = normalised
        product.barcode = product.barcode or payload.barcode

    # The barcode lookup already paid for Open Food Facts; persist the label
    # figure it returned instead of discarding it with the suggestion. Curated
    # seed data and admin edits outrank it, hence the default-only guard.
    if product.caffeine_source == "default":
        caffeine = _cached_caffeine(session, normalised)
        if caffeine is not None:
            product.caffeine_mg_per_100ml = caffeine
            product.caffeine_source = "openfoodfacts"

    observation_id = None
    if payload.price_cents and store is not None:
        photo_id = None
        if payload.photo_token:
            from ..models import Photo

            photo = session.scalar(select(Photo).where(Photo.token == payload.photo_token))
            photo_id = photo.id if photo else None
        observation = Observation(
            store_id=store.id,
            chain_id=store.chain_id,
            product_id=product.id,
            price_cents=payload.price_cents,
            source="crowd",
            price_scope="store",
            device_id=device.id,
            note=payload.note,
            photo_id=photo_id,
            status="needs_review",
        )
        session.add(observation)
        session.flush()
        observation_id = observation.id

    device.reputation = (device.reputation or 0) + (1 if created else 0)
    session.commit()

    return PendingProductOut(
        product=ProductOut.model_validate(product),
        created=created,
        observation_id=observation_id,
    )
