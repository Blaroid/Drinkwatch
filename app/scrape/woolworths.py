"""Woolworths scraper: the shop API is reachable after priming a cookie jar on the homepage.

POST https://www.woolworths.com.au/apis/ui/Search/products
body: {"SearchTerm": "...", "PageSize": 36, "PageNumber": 1, "SortType": "TraderRelevance",
       "IsSpecial": false}

Woolworths prices per state, and the search API answers from whatever store context
it infers unless one is named, so we pin a WA store (see WOOLWORTHS_STORE_ID) and
stamp the state on every chain-scope row.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Listing, Product, Store
from .base import HttpClient, ScrapeStats, chain_by_code, record_price
from .normalize import detect_brand, is_energy_item, parse_pack

log = logging.getLogger(__name__)

CHAIN_CODE = "woolworths"
BASE_URL = "https://www.woolworths.com.au"
SEARCH_URL = BASE_URL + "/apis/ui/Search/products"

DEFAULT_KEYWORDS = (
    "monster energy",
    "red bull energy drink",
    "v energy drink",
    "mother energy drink",
    "rockstar energy drink",
    "nos energy drink",
    "dare iced coffee",
    "prime hydration",
)


def prime_session(client: HttpClient) -> None:
    client.get(BASE_URL, headers={"Accept": "text/html"})


def search(
    client: HttpClient,
    term: str,
    page: int = 1,
    page_size: int = 36,
    store_id: str | None = None,
    settlement: str | None = None,
) -> list[dict]:
    payload = {
        "SearchTerm": term,
        "PageSize": page_size,
        "PageNumber": page,
        "SortType": "TraderRelevance",
        "IsSpecial": False,
        "Location": "/shop/search/products?searchTerm=" + term.replace(" ", "%20"),
    }
    # Without a store the response reflects whatever context the API picks, which
    # can be another state's catalogue. Both keys are optional because only one of
    # them is used by a given API build; send whichever the operator has.
    if store_id:
        payload["StoreId"] = store_id
    if settlement:
        payload["Settlement"] = settlement
    response = client.post(
        SEARCH_URL,
        json=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/shop/search/products?searchTerm=" + term.replace(" ", "%20"),
        },
    )
    data = response.json()
    flattened: list[dict] = []
    for group in data.get("Products", []) or []:
        for product in group.get("Products", []) or []:
            flattened.append(product)
    return flattened


def _upsert_listing(session: Session, chain_id: int, raw: dict, product: Product, pack_count: int) -> Listing:
    from datetime import datetime, timezone

    sku = str(raw.get("Stockcode") or raw.get("ProductID") or raw.get("UrlFriendlyName"))
    listing = session.scalar(
        select(Listing).where(Listing.chain_id == chain_id, Listing.external_sku == sku)
    )
    if listing is None:
        listing = Listing(chain_id=chain_id, external_sku=sku)
        session.add(listing)
    listing.product_id = product.id
    listing.url = f"{BASE_URL}/shop/productdetails/{raw.get('Stockcode')}"
    listing.title_raw = raw.get("DisplayName") or raw.get("Name")
    listing.size_text = raw.get("PackageSize")
    listing.pack_count = pack_count
    listing.barcode = raw.get("Barcode")
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
        stats.errors.append("chain 'woolworths' not seeded")
        return stats
    # Pin the search to a WA store. An unpinned run is recorded as unscoped
    # (state null) rather than assumed WA: the API's default context is exactly
    # what we cannot trust, and a null row keeps the old all-stores attribution
    # instead of stamping "WA" on a price that may belong to NSW.
    pin_store_id = settings.woolworths_store_id or None
    pin_settlement = settings.woolworths_settlement or None
    pinned = pin_store_id is not None or pin_settlement is not None
    state = settings.woolworths_state if pinned else None
    if not pinned:
        log.warning(
            "woolworths: WOOLWORTHS_STORE_ID/WOOLWORTHS_SETTLEMENT unset; prices are "
            "unscoped and may reflect another state's catalogue"
        )
    with HttpClient(browser=True, min_interval=1.2) as client:
        prime_session(client)
        for term in keywords or DEFAULT_KEYWORDS:
            try:
                results = search(
                    client, term, store_id=pin_store_id, settlement=pin_settlement
                )
            except Exception as exc:
                stats.errors.append(f"search '{term}': {exc}")
                continue
            stats.items_seen += len(results)
            for raw in results:
                name = raw.get("DisplayName") or raw.get("Name") or ""
                if not name:
                    continue
                price_dollars = raw.get("Price")
                if not price_dollars:
                    continue
                price_cents = round(float(price_dollars) * 100)
                was_price = raw.get("WasPrice")
                was_cents = round(float(was_price) * 100) if was_price else None
                size_ml, pack_count = parse_pack(raw.get("PackageSize") or name)
                if not is_energy_item(name, None, size_ml):
                    continue
                product, created = match_product_woolworths(
                    session, name=name, size_ml=size_ml, pack_count=pack_count,
                    barcode=raw.get("Barcode"),
                )
                if created:
                    stats.products_created += 1
                if product is None:
                    continue
                listing = _upsert_listing(session, chain.id, raw, product, pack_count)
                promo_kind = "special" if raw.get("IsSpecial") or (was_cents and was_cents > price_cents) else "none"
                if record_price(
                    session,
                    product=product,
                    chain_id=chain.id,
                    store_id=None,
                    price_cents=price_cents,
                    was_price_cents=was_cents if was_cents and was_cents > price_cents else None,
                    promo_kind=promo_kind,
                    in_stock=True if raw.get("IsAvailable") else None,
                    listing_id=listing.id,
                    price_scope="chain",
                    state=state,
                ):
                    stats.observations_written += 1
            session.commit()
    return stats


def match_product_woolworths(session: Session, *, name: str, size_ml, pack_count, barcode):
    from .normalize import match_product

    return match_product(
        session,
        name=name,
        size_ml=size_ml,
        pack_count=pack_count,
        barcode=str(barcode) if barcode else None,
        category="energy",
    )
