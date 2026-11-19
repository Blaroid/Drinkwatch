"""Coles scraper: no public API, but the Next.js data routes serve the same JSON the web app uses.

1. GET / -> scrape the buildId out of the HTML
2. GET /_next/data/<buildId>/search/products.json?q=<term>&page=1 with header x-nextjs-data: 1
"""

from __future__ import annotations

import logging
import pathlib
import re
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Listing, Product
from .base import CffiClient, ScrapeStats, chain_by_code, record_price
from .gtin import coles_page_has_product, enrich_listing, parse_coles_product
from .normalize import is_energy_item, match_product, parse_pack

log = logging.getLogger(__name__)

CHAIN_CODE = "coles"
BASE_URL = "https://www.coles.com.au"
PRODUCT_URL = BASE_URL + "/product/{sku}"
BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([^"]+)"')
BOT_WALL_RE = re.compile(r"Pardon Our Interruption|ewelcome-|Reference #", re.I)
BUILD_ID_CACHE = pathlib.Path("/tmp/coles_build_id")

DEFAULT_KEYWORDS = (
    "monster energy",
    "red bull",
    "v energy",
    "mother energy",
    "rockstar energy",
    "nos energy",
    "dare iced coffee",
    "prime hydration",
)


def fetch_build_id(client: CffiClient, attempts: int = 3) -> str:
    """Scrape the Next.js buildId, backing off politely if Akamai shows a bot wall."""
    last_reason = "unknown"
    for attempt in range(1, attempts + 1):
        html = client.get(BASE_URL, headers={"Accept": "text/html"}).text
        match = BUILD_ID_RE.search(html)
        if match:
            BUILD_ID_CACHE.write_text(match.group(1))
            return match.group(1)
        last_reason = "bot wall" if BOT_WALL_RE.search(html) else "buildId not in markup"
        log.warning("coles homepage attempt %s/%s: %s", attempt, attempts, last_reason)
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise RuntimeError(f"could not obtain buildId ({last_reason})")


def resolve_build_id(client: CffiClient) -> str:
    """Prefer the last known buildId: it usually still works, and skipping the
    homepage avoids the Akamai check that gates it."""
    if BUILD_ID_CACHE.exists():
        cached = BUILD_ID_CACHE.read_text().strip()
        if cached:
            try:
                if search(client, cached, "monster energy"):
                    log.info("coles using cached buildId %s", cached)
                    return cached
            except Exception:
                log.info("coles cached buildId %s no longer works", cached)
    return fetch_build_id(client)


def search(client: CffiClient, bid: str, term: str, page: int = 1) -> list[dict]:
    url = f"{BASE_URL}/_next/data/{bid}/search/products.json"
    response = client.get(
        url,
        params={"q": term, "page": page},
        headers={
            "Accept": "application/json",
            "x-nextjs-data": "1",
            "Referer": f"{BASE_URL}/search/products?q={term.replace(' ', '+')}",
        },
    )
    payload = response.json()
    return payload.get("pageProps", {}).get("searchResults", {}).get("results", []) or []


def fetch_barcode(client: CffiClient, sku: str) -> str | None:
    """Read the GTIN from the product page the app links to.

    Coles' search results carry no barcode/GTIN field at all. The product page
    does, in both its schema.org JSON-LD and the Next.js pageProps, so a page
    fetch per uncoded SKU is the only route. A bot wall must raise rather than
    look like "this product has no code", or the memo would hide it for a month.
    """
    response = client.get(
        PRODUCT_URL.format(sku=sku),
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": BASE_URL + "/search/products",
        },
    )
    if not coles_page_has_product(response.text):
        raise RuntimeError("coles product page did not contain a product (bot wall?)")
    return parse_coles_product(response.text)


def _upsert_listing(session: Session, chain_id: int, raw: dict, product: Product, pack_count: int) -> Listing:
    sku = str(raw.get("id"))
    listing = session.scalar(
        select(Listing).where(Listing.chain_id == chain_id, Listing.external_sku == sku)
    )
    if listing is None:
        listing = Listing(chain_id=chain_id, external_sku=sku)
        session.add(listing)
    listing.product_id = product.id
    listing.url = f"{BASE_URL}/product/{sku}"
    listing.title_raw = f"{raw.get('brand', '')} {raw.get('name', '')}".strip()
    listing.size_text = raw.get("size")
    listing.pack_count = pack_count
    listing.last_seen_at = datetime.now(timezone.utc)
    session.flush()
    return listing


def scrape(
    session: Session,
    store_ids: list[int] | None = None,
    keywords: list[str] | None = None,
    stats: ScrapeStats | None = None,
) -> ScrapeStats:
    stats = stats or ScrapeStats(chain=CHAIN_CODE)
    chain = chain_by_code(session, CHAIN_CODE)
    if chain is None:
        stats.errors.append("chain 'coles' not seeded")
        return stats
    barcode_budget = settings.barcode_lookup_limit
    with CffiClient(min_interval=1.5) as client:
        try:
            bid = resolve_build_id(client)
        except Exception as exc:
            stats.errors.append(f"buildId: {exc}")
            return stats
        log.info("coles buildId=%s", bid)

        for term in keywords or DEFAULT_KEYWORDS:
            try:
                results = search(client, bid, term)
            except Exception as exc:
                stats.errors.append(f"search '{term}': {exc}")
                continue
            stats.items_seen += len(results)
            for raw in results:
                if raw.get("_type") != "PRODUCT":
                    continue
                pricing = raw.get("pricing") or {}
                now = pricing.get("now")
                if not now:
                    continue
                price_cents = round(float(now) * 100)
                was = pricing.get("was") or 0
                was_cents = round(float(was) * 100) if was and was > now else None

                title = f"{raw.get('brand', '')} {raw.get('name', '')}".strip()
                size_ml, pack_count = parse_pack(f"{title} {raw.get('size') or ''}")
                if not is_energy_item(title, raw.get("brand") or None, size_ml):
                    continue
                product, created = match_product(
                    session,
                    name=title,
                    brand=raw.get("brand") or None,
                    size_ml=size_ml,
                    pack_count=pack_count,
                    category="energy",
                )
                if created:
                    stats.products_created += 1
                if product is None:
                    continue
                listing = _upsert_listing(session, chain.id, raw, product, pack_count)
                had_barcode = bool(listing.barcode)
                try:
                    code, barcode_budget = enrich_listing(
                        session,
                        listing,
                        product,
                        chain=CHAIN_CODE,
                        source="coles-detail",
                        budget=barcode_budget,
                        fetch=lambda sku: fetch_barcode(client, sku),
                    )
                except Exception as exc:  # network: retry next run, do not memoise
                    log.warning("coles barcode %s: %s", listing.external_sku, exc)
                else:
                    if code and not had_barcode:
                        stats.barcodes_found += 1

                promo_kind = "none"
                promo_qty = None
                promo_total_cents = None
                multi = pricing.get("multiBuyPromotion")
                if multi:
                    promo_kind = "multibuy"
                    promo_qty = multi.get("minQuantity")
                    reward = multi.get("reward")
                    if reward and promo_qty:
                        promo_total_cents = round(float(reward) * int(promo_qty) * 100)
                elif was_cents or pricing.get("promotionType") == "SPECIAL":
                    promo_kind = "special"

                availability = raw.get("availabilityType") or ""
                in_stock = True if "Online" in availability or "InStore" in availability else None

                if record_price(
                    session,
                    product=product,
                    chain_id=chain.id,
                    store_id=None,
                    price_cents=price_cents,
                    was_price_cents=was_cents,
                    promo_kind=promo_kind,
                    promo_qty=promo_qty,
                    promo_total_cents=promo_total_cents,
                    in_stock=in_stock,
                    listing_id=listing.id,
                    price_scope="chain",
                ):
                    stats.observations_written += 1
            session.commit()
    return stats
