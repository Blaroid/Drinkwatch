"""Find the GTIN a retailer publishes only on its product-detail response.

Barcode-first onboarding needs real printed codes, but both supermarket search
payloads carry a ``Barcode`` key that is null on every tile. Woolworths exposes
the code on ``/apis/ui/product/detail/<stockcode>`` and Coles on the product page
(the Next.js ``pageProps.product.gtin`` and the schema.org JSON-LD agree), so a
detail request per SKU is the only way to learn it. SpudShed publishes no GTIN
at all.

A code is only accepted when its mod-10 check digit is valid: the v0.5.2 purge
showed how much noise internal/synthetic codes add, and a wrong mapping is worse
than a missing one for scanning.

The memo in ``barcode_cache`` is keyed by chain + SKU (not by barcode) and keeps
a miss from being re-fetched every scrape. The TTL lets a code that appears
later be picked up again; ``scripts/backfill_barcodes.py --refresh`` bypasses it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from selectolax.parser import HTMLParser
from sqlalchemy import select

from .normalize import ean13_checksum_ok

MEMO_TTL_DAYS = 30
_MEMO_PREFIX = {"woolworths": "ww", "coles": "coles"}


def plausible_gtin(raw: Any) -> str | None:
    """The digits of a real GTIN (8/12/13/14), or None for junk.

    Validate before normalising: a GTIN-14's check digit covers its packaging
    indicator, so stripping the leading "1" produces a number whose checksum no
    longer holds even though the retailer's code is genuine.
    """
    if raw is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(raw))
    if len(digits) not in {8, 12, 13, 14}:
        return None
    if not ean13_checksum_ok(digits):
        return None
    return digits


def parse_woolworths_detail(payload: dict) -> str | None:
    """GTIN from ``GET /apis/ui/product/detail/<stockcode>``.

    The search response has the same field name but always null; only this
    detail payload fills it in.
    """
    product = payload.get("Product") if isinstance(payload, dict) else None
    if not isinstance(product, dict):
        return None
    return plausible_gtin(product.get("Barcode"))


def _json_ld_products(html: str) -> list[dict]:
    tree = HTMLParser(html)
    products: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (TypeError, ValueError):
            continue
        entries = data if isinstance(data, list) else [data]
        products.extend(
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("@type") == "Product"
        )
    return products


def _next_data_product(html: str) -> dict | None:
    tree = HTMLParser(html)
    node = tree.css_first("script#__NEXT_DATA__")
    if node is None:
        return None
    try:
        data = json.loads(node.text())
    except (TypeError, ValueError):
        return None
    product = ((data.get("props") or {}).get("pageProps") or {}).get("product")
    if not isinstance(product, dict) or not product.get("id"):
        return None
    return product


def parse_coles_product(html: str) -> str | None:
    """GTIN from a Coles ``/product/<id>`` page.

    Read the schema.org JSON-LD first because it is the stable public contract;
    fall back to the Next.js data blob in case the JSON-LD block moves.
    """
    for product in _json_ld_products(html):
        code = plausible_gtin(product.get("gtin"))
        if code:
            return code
    product = _next_data_product(html)
    return plausible_gtin(product.get("gtin")) if product else None


def coles_page_has_product(html: str) -> bool:
    """Whether the HTML is a product page at all.

    Akamai serves a challenge page with HTTP 200, and its "ewelcome-" script tag
    also appears on valid pages, so the bot wall cannot be spotted by substring.
    A real page carries the product in JSON-LD or ``pageProps``; without one a
    missing GTIN is not a fact about the product and must not be memoised.
    """
    if _next_data_product(html) is not None:
        return True
    return bool(_json_ld_products(html))


def memo_key(chain: str, sku: str) -> str:
    """A ``barcode_cache`` key for a retailer SKU, inside its 20-char column.

    Numeric barcodes never start with a chain prefix, so these rows cannot
    collide with the Open Food Facts cache in ``app/api/barcode.py``.
    """
    prefix = _MEMO_PREFIX.get(chain, chain[:5])
    return f"{prefix}:{sku}"[:20]


def cached_sighting(session, key: str) -> tuple[bool, str | None]:
    """Return ``(already_checked, barcode)`` for a SKU, honouring the TTL."""
    from ..models import BarcodeCache

    row = session.get(BarcodeCache, key)
    if row is None:
        return False, None
    created = row.created_at
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < datetime.now(timezone.utc) - timedelta(days=MEMO_TTL_DAYS):
            return False, None
    code = (row.payload or {}).get("barcode")
    return True, code or None


def remember_sighting(session, key: str, barcode: str | None, source: str) -> None:
    """Record a positive *or* negative detail lookup so the next run can skip it."""
    from ..models import BarcodeCache

    now = datetime.now(timezone.utc)
    row = session.get(BarcodeCache, key)
    if row is None:
        session.add(
            BarcodeCache(
                barcode=key,
                source=source,
                payload={"barcode": barcode},
                created_at=now,
            )
        )
        return
    row.source = source
    row.payload = {"barcode": barcode}
    row.created_at = now


def record_listing_barcode(session, listing, product, raw: Any, source: str) -> str | None:
    """Store a retailer-published code on the listing and in the product index.

    The raw retailer form is kept on the listing because packs and single cans
    can carry different codes for the same product; ``register_barcode`` indexes
    every variant so the scan can arrive in any form.
    """
    from .normalize import normalize_barcode, register_barcode

    code = plausible_gtin(raw)
    if code is None:
        return None
    listing.barcode = code
    if product is None:
        return code
    register_barcode(session, product.id, code, source=source)
    # Never replace a code another retailer already published for this product:
    # both are real, and the index exists to hold every form. Also refuse a code
    # another product already owns (duplicate catalogue rows are common), or the
    # unique products.barcode constraint would abort the whole scrape.
    if not product.barcode:
        from ..models import Product

        owner = session.scalar(
            select(Product.id).where(Product.barcode == code, Product.id != product.id)
        )
        if owner is None:
            product.barcode = code
    normalised = normalize_barcode(code)
    if normalised and not product.normalised_barcode:
        product.normalised_barcode = normalised
    return code


def enrich_listing(
    session,
    listing,
    product,
    *,
    chain: str,
    source: str,
    budget: int,
    fetch: Callable[[str], str | None],
    refresh: bool = False,
) -> tuple[str | None, int]:
    """Index a barcode for one listing, spending at most one detail request.

    Returns ``(code, remaining_budget)``. Only a completed lookup (found or
    genuinely absent) is memoised; a network/HTTP failure raises out of
    ``fetch`` so the caller can retry on the next run instead of remembering a
    transient error as "this can has no code". Listings whose product already
    has a code are skipped: scanning that code already resolves the product.
    """
    if product is None:
        return None, budget
    if listing.barcode:
        code = record_listing_barcode(session, listing, product, listing.barcode, source)
        if code:
            return code, budget
    if product.barcode or product.normalised_barcode:
        return None, budget

    key = memo_key(chain, listing.external_sku)
    checked, code = cached_sighting(session, key) if not refresh else (False, None)
    if not checked:
        if budget <= 0:
            return None, budget
        code = fetch(listing.external_sku)
        budget -= 1
        remember_sighting(session, key, code, source)
    if not code:
        return None, budget
    return record_listing_barcode(session, listing, product, code, source), budget
