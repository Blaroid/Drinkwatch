"""Admin API: scraping control, curation, moderation, stats."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..caffeine import apply_reference_caffeine
from ..daily import sync_daily_price
from ..db import get_session
from ..models import (
    Barcode,
    Chain,
    Device,
    Listing,
    Observation,
    Photo,
    Product,
    Report,
    ScrapeRun,
    Store,
)
from ..security import require_admin
from .deps import utcnow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class ScrapeRequest(BaseModel):
    chains: list[str] | None = Field(default=None, description="chain codes; empty = all enabled")
    store_ids: list[int] | None = None


class MapListingRequest(BaseModel):
    product_slug: str


class ProductCreateRequest(BaseModel):
    brand: str = Field(max_length=60)
    name: str = Field(max_length=160)
    variant: str | None = Field(default=None, max_length=80)
    size_ml: int | None = Field(default=None, ge=50, le=5000)
    pack_count: int = Field(default=1, ge=1, le=48)
    barcode: str | None = Field(default=None, max_length=32)
    category: str = Field(default="energy", max_length=40)


class LinkRequest(BaseModel):
    target_slug: str
    unlink: bool = False


class StatusRequest(BaseModel):
    status: str = Field(pattern="^(accepted|needs_review|rejected)$")


@router.get("/stats")
def stats(session: Session = Depends(get_session)) -> dict:
    def count(table) -> int:
        return int(session.scalar(select(func.count()).select_from(table)) or 0)

    # Stores and observations are counted in their own subqueries. Joining both
    # onto Chain in one query multiplies each observation by the number of stores
    # in the chain, which read as ~30x too many rows on the live database.
    per_chain = session.execute(
        select(
            Chain.code,
            select(func.count(Store.id))
            .where(Store.chain_id == Chain.id)
            .scalar_subquery()
            .label("stores"),
            select(func.count(Observation.id))
            .where(Observation.chain_id == Chain.id)
            .scalar_subquery()
            .label("observations"),
        ).order_by(Chain.code)
    ).all()
    per_source = session.execute(
        select(Observation.source, func.count()).group_by(Observation.source)
    ).all()
    return {
        "time": utcnow(),
        "counts": {
            "chains": count(Chain),
            "stores": count(Store),
            "products": count(Product),
            "observations": count(Observation),
            "devices": count(Device),
            "reports": count(Report),
        },
        "per_chain": [
            {"chain": row[0], "stores": int(row[1] or 0), "observations": int(row[2] or 0)}
            for row in per_chain
        ],
        "per_source": {row[0]: int(row[1]) for row in per_source},
        "unmapped_listings": int(
            session.scalar(
                select(func.count()).select_from(Listing).where(Listing.product_id.is_(None))
            )
            or 0
        ),
        "needs_review": int(
            session.scalar(
                select(func.count()).select_from(Observation).where(Observation.status == "needs_review")
            )
            or 0
        ),
        "last_runs": [
            {
                "chain": run.chain_code,
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "items_seen": run.items_seen,
                "written": run.observations_written,
                "error": run.error,
            }
            for run in session.scalars(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(10))
        ],
    }


@router.post("/scrape/run", status_code=status.HTTP_202_ACCEPTED)
def trigger_scrape(
    payload: ScrapeRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> dict:
    from ..scrape.runner import run_chains

    codes = payload.chains
    if not codes:
        codes = [
            chain.code
            for chain in session.scalars(select(Chain).where(Chain.scrape_enabled.is_(True)))
        ]
    if not codes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no chains enabled for scraping")
    background.add_task(run_chains, codes, payload.store_ids)
    return {"queued": codes}


@router.get("/runs")
def runs(limit: int = Query(default=50, ge=1, le=200), session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "id": run.id,
            "chain": run.chain_code,
            "store_id": run.store_id,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "items_seen": run.items_seen,
            "observations_written": run.observations_written,
            "products_created": run.products_created,
            "error": run.error,
        }
        for run in session.scalars(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(limit))
    ]


@router.get("/listings")
def listings(
    unmapped: bool = Query(default=False),
    chain: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(Listing).join(Chain).order_by(Listing.id.desc()).limit(limit)
    if unmapped:
        stmt = stmt.where(Listing.product_id.is_(None))
    if chain:
        stmt = stmt.where(Chain.code == chain)
    return [
        {
            "id": row.id,
            "chain": row.chain_id,
            "external_sku": row.external_sku,
            "title_raw": row.title_raw,
            "size_text": row.size_text,
            "pack_count": row.pack_count,
            "barcode": row.barcode,
            "product_id": row.product_id,
            "url": row.url,
        }
        for row in session.scalars(stmt)
    ]


@router.post("/listings/{listing_id}/map")
def map_listing(
    listing_id: int, payload: MapListingRequest, session: Session = Depends(get_session)
) -> dict:
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown listing")
    product = session.scalar(select(Product).where(Product.slug == payload.product_slug))
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown product")
    listing.product_id = product.id
    session.commit()
    return {"listing_id": listing.id, "product_id": product.id, "product_slug": product.slug}


def _code_forms(raw: str) -> list[str]:
    """Every key one physical code can occupy in the barcodes index.

    deps.find_product_by_barcode() accepts whichever form a client scanned, so
    a moderator acting on one form has to see the code's other forms too.
    """
    from ..scrape.normalize import barcode_variants

    return barcode_variants(raw) or [raw]


def _release_product_code(product: Product | None, forms: list[str]) -> list[str]:
    """Clear a product's own barcode columns when they name one of ``forms``.

    products.barcode and products.normalised_barcode resolve a scan on their
    own, so a binding removed from the index would keep working through them.
    """
    if product is None:
        return []
    released = []
    if product.barcode in forms:
        product.barcode = None
        released.append("barcode")
    if product.normalised_barcode in forms:
        product.normalised_barcode = None
        released.append("normalised_barcode")
    return released


def _other_claimants(session: Session, product_id: int, forms: list[str]) -> list[Product]:
    """Products whose own barcode columns resolve one of ``forms``.

    Both products.barcode and products.normalised_barcode win a scan without the
    barcodes table, so a moderator acting on a code has to account for them too.
    """
    return list(
        session.scalars(
            select(Product).where(
                Product.id != product_id,
                (Product.barcode.in_(forms)) | (Product.normalised_barcode.in_(forms)),
            )
        )
    )


def _claim_product_code(session: Session, product: Product, raw: str) -> bool:
    """Make a mapped code the product's own code when nobody else claims it.

    Writing the columns while another product still resolves the same code would
    leave a scan with two answers, so the index rows alone carry the mapping
    until the conflict is resolved.
    """
    from ..scrape.normalize import normalize_barcode

    forms = _code_forms(raw)
    if _other_claimants(session, product.id, forms):
        return False
    changed = False
    normalised = normalize_barcode(raw)
    if normalised and not product.normalised_barcode:
        product.normalised_barcode = normalised
        changed = True
    if not product.barcode:
        product.barcode = raw
        changed = True
    return changed


class BarcodeMapRequest(BaseModel):
    product_slug: str = Field(max_length=160)


@router.get("/barcodes", summary="Barcode index rows, newest first")
def barcodes(
    product_slug: str | None = None,
    source: str | None = Query(default=None, description="scrape | crowd | admin"),
    barcode: str | None = Query(default=None, description="any form of one physical code"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = (
        select(Barcode, Product)
        .join(Product, Product.id == Barcode.product_id)
        .order_by(Barcode.created_at.desc(), Barcode.barcode)
        .limit(limit)
    )
    if product_slug:
        stmt = stmt.where(Product.slug == product_slug)
    if source:
        stmt = stmt.where(Barcode.source == source)
    if barcode:
        stmt = stmt.where(Barcode.barcode.in_(_code_forms(barcode)))
    return [
        {
            "barcode": row.barcode,
            "product_id": product.id,
            "product_slug": product.slug,
            "product_label": f"{product.brand} {product.name}".strip(),
            "product_status": product.status,
            "source": row.source,
            "created_at": row.created_at,
        }
        for row, product in session.execute(stmt)
    ]


@router.delete("/barcodes/{barcode}", summary="Remove a barcode binding")
def delete_barcode(barcode: str, session: Session = Depends(get_session)) -> dict:
    """Unbind a code so scanning it stops resolving to that product.

    Every form of the code goes, because the lookup accepts whichever form the
    client scanned: deleting one row while a sibling form survived would leave
    the binding working. A form bound to a different product is reported and
    left for its own decision.
    """
    forms = _code_forms(barcode)
    rows = list(session.scalars(select(Barcode).where(Barcode.barcode.in_(forms))))
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown barcode")
    target = next(
        (row.product_id for row in rows if row.barcode == barcode), rows[0].product_id
    )
    removed = [row.barcode for row in rows if row.product_id == target]
    product = session.get(Product, target)
    released = _release_product_code(product, forms)
    others = {row.barcode: row.product_id for row in rows if row.product_id != target}
    # A product that carries the code in its own columns keeps resolving a scan
    # after the index row is gone, so report it rather than leaving the moderator
    # believing the binding is dead.
    for holder in _other_claimants(session, target, forms):
        for form in (holder.barcode, holder.normalised_barcode):
            if form in forms:
                others.setdefault(form, holder.id)
    for row in rows:
        if row.product_id == target:
            session.delete(row)
    session.commit()
    return {
        "barcode": barcode,
        "removed": removed,
        "product_slug": product.slug if product is not None else None,
        "product_columns_cleared": released,
        "other_products": others,
    }


@router.post("/barcodes/{barcode}/map", summary="Bind or move a barcode onto a product")
def map_barcode(
    barcode: str, payload: BarcodeMapRequest, session: Session = Depends(get_session)
) -> dict:
    """Point a code at a product, creating the binding if it is new.

    This is the repair path for a crowd binding that landed on the wrong can, and
    the way to add a retailer-internal code that the crowd flow refuses.
    """
    product = session.scalar(select(Product).where(Product.slug == payload.product_slug))
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown product")
    forms = _code_forms(barcode)
    # A scan resolves through the index rows and through products.barcode /
    # normalised_barcode alike, so a move has to take the code away from every
    # product that currently answers it - including one that only carries it in
    # its own columns, which no barcode row would have revealed.
    holders = set(session.scalars(select(Barcode.product_id).where(Barcode.barcode.in_(forms))))
    holders |= {holder.id for holder in _other_claimants(session, product.id, forms)}
    holders.discard(product.id)
    for holder_id in sorted(holders):
        _release_product_code(session.get(Product, holder_id), forms)
    created: list[str] = []
    moved: list[str] = []
    for form in forms:
        row = session.get(Barcode, form)
        if row is None:
            session.add(Barcode(barcode=form, product_id=product.id, source="admin"))
            created.append(form)
        elif row.product_id != product.id:
            row.product_id = product.id
            moved.append(form)
    columns = _claim_product_code(session, product, barcode)
    session.commit()
    return {
        "barcode": barcode,
        "product_id": product.id,
        "product_slug": product.slug,
        "created": created,
        "moved": moved,
        "product_columns_written": columns,
    }


@router.get("/products/links", summary="Current cross-chain product groups")
def product_links(session: Session = Depends(get_session)) -> list[dict]:
    from ..scrape.normalize import flavour_tokens

    roots = {
        product.id: product
        for product in session.scalars(select(Product).where(Product.canonical_product_id.is_(None)))
    }
    groups: dict[int, list[Product]] = {}
    for product in session.scalars(select(Product)):
        root_id = product.canonical_product_id or product.id
        groups.setdefault(root_id, []).append(product)
    out = []
    for root_id, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        root = roots.get(root_id) or members[0]
        out.append(
            {
                "root_slug": root.slug,
                "root_label": f"{root.brand} {root.name}".strip(),
                "size_ml": root.size_ml,
                "pack_count": root.pack_count,
                "members": [
                    {"slug": member.slug, "label": f"{member.brand} {member.name}".strip()}
                    for member in members
                    if member.id != root_id
                ],
                "tokens": sorted(flavour_tokens(f"{root.name} {root.variant or ''}")),
            }
        )
    return out


@router.get("/products/suggestions", summary="Candidate cross-chain product links")
def product_suggestions(
    brand: str | None = None,
    min_score: float = Query(default=0.34, ge=0.0, le=1.0),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    """Suggest pairs of product rows that are probably the same can.

    Nothing is linked automatically: names alone are not reliable (Monster "Ultra"
    vs "Ultra Violet"), so a human confirms each pair via /products/{slug}/link.
    """
    from ..scrape.normalize import flavour_tokens

    stmt = select(Product).where(Product.canonical_product_id.is_(None))
    if brand:
        stmt = stmt.where(func.lower(Product.brand) == brand.lower())
    products = list(session.scalars(stmt))

    chains_by_product: dict[int, set[str]] = {}
    for product_id, code in session.execute(
        select(Observation.product_id, Chain.code).join(Chain, Chain.id == Observation.chain_id).distinct()
    ):
        chains_by_product.setdefault(product_id, set()).add(code)

    buckets: dict[tuple[str, int | None, int], list[Product]] = {}
    for product in products:
        buckets.setdefault(
            (product.brand.lower(), product.size_ml, product.pack_count), []
        ).append(product)

    suggestions = []
    for (_, _, _), members in buckets.items():
        if len(members) < 2:
            continue
        for index, left in enumerate(members):
            left_tokens = flavour_tokens(f"{left.name} {left.variant or ''}")
            for right in members[index + 1 :]:
                if (left.canonical_product_id or left.id) == (right.canonical_product_id or right.id):
                    continue
                right_tokens = flavour_tokens(f"{right.name} {right.variant or ''}")
                union = left_tokens | right_tokens
                score = len(left_tokens & right_tokens) / len(union) if union else 0.0
                if score < min_score:
                    continue
                suggestions.append(
                    {
                        "score": round(score, 2),
                        "size_ml": left.size_ml,
                        "pack_count": left.pack_count,
                        "left": {
                            "slug": left.slug,
                            "label": f"{left.brand} {left.name}".strip(),
                            "chains": sorted(chains_by_product.get(left.id, set())),
                        },
                        "right": {
                            "slug": right.slug,
                            "label": f"{right.brand} {right.name}".strip(),
                            "chains": sorted(chains_by_product.get(right.id, set())),
                        },
                    }
                )
    suggestions.sort(key=lambda item: item["score"], reverse=True)
    return suggestions[:limit]


@router.post("/products/{slug}/link", summary="Link a product row to a canonical group")
def link_product(
    slug: str, payload: LinkRequest, session: Session = Depends(get_session)
) -> dict:
    product = session.scalar(select(Product).where(Product.slug == slug))
    target = session.scalar(select(Product).where(Product.slug == payload.target_slug))
    if product is None or target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown product slug")
    if product.id == target.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot link a product to itself")
    if payload.unlink:
        product.canonical_product_id = None
    else:
        root = target.canonical_product_id or target.id
        if root == product.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "link would create a cycle")
        product.canonical_product_id = root
    session.commit()
    return {
        "slug": product.slug,
        "canonical_product_id": product.canonical_product_id,
        "target_slug": target.slug,
    }


@router.post("/products", status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreateRequest, session: Session = Depends(get_session)) -> dict:
    from ..scrape.normalize import build_slug

    slug = build_slug(payload.brand, payload.name, payload.variant, payload.size_ml, payload.pack_count)
    if session.scalar(select(Product).where(Product.slug == slug)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"product already exists: {slug}")
    product = Product(
        slug=slug,
        brand=payload.brand,
        name=payload.name,
        variant=payload.variant,
        size_ml=payload.size_ml,
        pack_count=payload.pack_count,
        barcode=payload.barcode,
        category=payload.category,
        source="manual",
    )
    # Manual products are still SKUs with a printed label; seed the caffeine
    # the same way a scrape would, so "manual" is not a second unset path.
    apply_reference_caffeine(session, product)
    session.add(product)
    session.commit()
    return {"id": product.id, "slug": product.slug}


@router.post("/observations/{observation_id}/status")
def set_observation_status(
    observation_id: int, payload: StatusRequest, session: Session = Depends(get_session)
) -> dict:
    observation = session.get(Observation, observation_id)
    if observation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown observation")
    before = observation.status
    observation.status = payload.status
    # Re-approving an accepted row is not a no-op: rows accepted before the
    # ledger followed the status may still be missing from price_daily, and this
    # is the moderator's way to repair one.
    if payload.status == "accepted" or payload.status != before:
        sync_daily_price(session, observation)
    session.commit()
    return {"id": observation.id, "status": observation.status}


@router.get("/devices")
def devices(limit: int = Query(default=100, ge=1, le=500), session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "id": device.id,
            "display_name": device.display_name,
            "platform": device.platform,
            "reputation": device.reputation,
            "is_banned": device.is_banned,
            "created_at": device.created_at,
            "last_seen_at": device.last_seen_at,
        }
        for device in session.scalars(select(Device).order_by(Device.id.desc()).limit(limit))
    ]


@router.get("/products/pending", summary="Crowd-submitted products awaiting review")
def pending_products(
    limit: int = Query(default=100, ge=1, le=500), session: Session = Depends(get_session)
) -> list[dict]:
    """The product queue, with the crowd sightings a decision has to judge.

    A moderator cannot decide from a name and a count: the price, the store, the
    device and the tag photo are the evidence. Returning them with the product
    keeps ``scripts/review.py queue`` to one round trip per list.
    """
    products = list(
        session.scalars(
            select(Product)
            .where(Product.status == "pending")
            .order_by(Product.id.desc())
            .limit(limit)
        )
    )
    if not products:
        return []

    sightings: dict[int, list[dict]] = {product.id: [] for product in products}
    rows = session.execute(
        select(Observation, Store.name, Store.suburb, Photo.token)
        .outerjoin(Store, Store.id == Observation.store_id)
        .outerjoin(Photo, Photo.id == Observation.photo_id)
        .where(Observation.product_id.in_(list(sightings)))
        .order_by(Observation.observed_at.desc(), Observation.id.desc())
    ).all()
    for observation, store_name, suburb, photo_token in rows:
        sightings[observation.product_id].append(
            {
                "id": observation.id,
                "price_cents": observation.price_cents,
                "status": observation.status,
                "source": observation.source,
                "store": store_name,
                "suburb": suburb,
                "device_id": observation.device_id,
                "note": observation.note,
                "photo_url": observation.photo_url,
                "photo_token": photo_token,
                "observed_at": observation.observed_at,
            }
        )

    return [
        {
            "slug": product.slug,
            "brand": product.brand,
            "name": product.name,
            "size_ml": product.size_ml,
            "pack_count": product.pack_count,
            "barcode": product.normalised_barcode or product.barcode,
            "observations": len(sightings[product.id]),
            "sightings": sightings[product.id],
            "created_at": product.created_at,
        }
        for product in products
    ]


@router.get("/observations", summary="Price rows awaiting a moderation decision")
def observations(
    status_filter: str = Query(
        default="needs_review",
        alias="status",
        pattern="^(accepted|needs_review|rejected)$",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    """Observations outside the pending-product queue (outliers on known cans).

    The pending product list only shows rows for products nobody has curated yet;
    a crowd price on an existing product can still be flagged by the outlier
    check, and that decision lives here.
    """
    rows = session.execute(
        select(
            Observation,
            Product.slug,
            Product.brand,
            Product.name,
            Store.name,
            Device.display_name,
            Photo.token,
        )
        .join(Product, Product.id == Observation.product_id)
        .outerjoin(Store, Store.id == Observation.store_id)
        .outerjoin(Device, Device.id == Observation.device_id)
        .outerjoin(Photo, Photo.id == Observation.photo_id)
        .where(Observation.status == status_filter)
        .order_by(Observation.observed_at.desc(), Observation.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": observation.id,
            "product_slug": slug,
            "product_label": f"{brand} {name}".strip(),
            "price_cents": observation.price_cents,
            "status": observation.status,
            "source": observation.source,
            "store": store_name,
            "device": device_name,
            "note": observation.note,
            "photo_url": observation.photo_url,
            "photo_token": photo_token,
            "observed_at": observation.observed_at,
        }
        for observation, slug, brand, name, store_name, device_name, photo_token in rows
    ]


class ApproveRequest(BaseModel):
    caffeine_mg_per_100ml: float | None = Field(default=None, ge=0, le=200)
    size_ml: int | None = Field(default=None, ge=50, le=5000)
    pack_count: int | None = Field(default=None, ge=1, le=48)
    merge_into_slug: str | None = None


@router.post("/products/{slug}/approve", summary="Confirm a crowd-submitted product")
def approve_product(
    slug: str, payload: ApproveRequest, session: Session = Depends(get_session)
) -> dict:
    product = session.scalar(select(Product).where(Product.slug == slug))
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown product")

    if payload.size_ml is not None:
        product.size_ml = payload.size_ml
    if payload.pack_count is not None:
        product.pack_count = payload.pack_count
    if payload.caffeine_mg_per_100ml is not None:
        product.caffeine_mg_per_100ml = payload.caffeine_mg_per_100ml
        product.caffeine_source = "admin"
    elif product.caffeine_source == "default":
        # Approval is a natural curation point: fill the label value if the
        # scraper/crowd row never had one, without touching an admin override.
        apply_reference_caffeine(session, product)
    product.status = "verified"

    if payload.merge_into_slug:
        target = session.scalar(select(Product).where(Product.slug == payload.merge_into_slug))
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown merge target")
        product.canonical_product_id = target.canonical_product_id or target.id

    product.family_slug = product.family_slug or __import__("re").sub(r"-x[0-9]+$", "", product.slug)
    session.commit()
    return {
        "slug": product.slug,
        "status": product.status,
        "canonical_product_id": product.canonical_product_id,
    }


@router.post("/products/{slug}/reject", summary="Reject a crowd-submitted product")
def reject_product(slug: str, session: Session = Depends(get_session)) -> dict:
    product = session.scalar(select(Product).where(Product.slug == slug))
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown product")
    product.status = "rejected"
    product.is_active = False
    session.commit()
    return {"slug": product.slug, "status": product.status}


@router.get("/photos", summary="Recent uploaded photos for moderation")
def recent_photos(
    limit: int = Query(default=50, ge=1, le=200), session: Session = Depends(get_session)
) -> list[dict]:
    """List recent uploads, with the sighting each one backs.

    A photo can be attached to more than one sighting; the newest one is
    reported, because that is the row a reviewer opening the photo from the
    moderation queue would expect to land on.
    """
    from ..models import Photo

    photos = list(session.scalars(select(Photo).order_by(Photo.id.desc()).limit(limit)))
    photo_ids = [photo.id for photo in photos]
    sightings: dict[int, Observation] = {}
    if photo_ids:
        for observation in session.scalars(
            select(Observation)
            .where(Observation.photo_id.in_(photo_ids))
            .order_by(Observation.observed_at.desc(), Observation.id.desc())
        ):
            sightings.setdefault(observation.photo_id, observation)
    out = []
    for photo in photos:
        linked = sightings.get(photo.id)
        out.append(
            {
                "id": photo.id,
                "token": photo.token,
                "url": f"/v1/photos/{photo.token}",
                "uploaded_by": photo.uploaded_by,
                "bytes": photo.bytes,
                "created_at": photo.created_at,
                "pruned_at": photo.pruned_at,
                "observation_id": linked.id if linked is not None else None,
                "product_id": linked.product_id if linked is not None else None,
                "store_id": linked.store_id if linked is not None else None,
            }
        )
    return out


@router.post("/alerts/evaluate", summary="Run the price-alert evaluation now")
def run_alerts(session: Session = Depends(get_session)) -> dict:
    from ..notify import evaluate_alerts

    return {"notifications_created": evaluate_alerts(session)}
