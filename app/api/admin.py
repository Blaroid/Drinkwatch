"""Admin API: scraping control, curation, moderation, stats."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..caffeine import apply_reference_caffeine
from ..db import get_session
from ..models import Chain, Device, Listing, Observation, Product, Report, ScrapeRun, Store
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

    per_chain = session.execute(
        select(
            Chain.code,
            func.count(func.distinct(Store.id)).label("stores"),
            func.count(Observation.id).label("observations"),
        )
        .select_from(Chain)
        .outerjoin(Store, Store.chain_id == Chain.id)
        .outerjoin(Observation, Observation.chain_id == Chain.id)
        .group_by(Chain.code)
        .order_by(Chain.code)
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
    observation.status = payload.status
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
    rows = session.execute(
        select(Product, func.count(Observation.id))
        .outerjoin(Observation, Observation.product_id == Product.id)
        .where(Product.status == "pending")
        .group_by(Product.id)
        .order_by(Product.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "slug": product.slug,
            "brand": product.brand,
            "name": product.name,
            "size_ml": product.size_ml,
            "pack_count": product.pack_count,
            "barcode": product.normalised_barcode or product.barcode,
            "observations": int(count or 0),
            "created_at": product.created_at,
        }
        for product, count in rows
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
    from ..models import Photo

    return [
        {
            "id": photo.id,
            "token": photo.token,
            "url": f"/v1/photos/{photo.token}",
            "uploaded_by": photo.uploaded_by,
            "bytes": photo.bytes,
            "created_at": photo.created_at,
        }
        for photo in session.scalars(select(Photo).order_by(Photo.id.desc()).limit(limit))
    ]


@router.post("/alerts/evaluate", summary="Run the price-alert evaluation now")
def run_alerts(session: Session = Depends(get_session)) -> dict:
    from ..notify import evaluate_alerts

    return {"notifications_created": evaluate_alerts(session)}
