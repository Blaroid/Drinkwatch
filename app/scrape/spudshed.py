"""SpudShed scraper: each store is its own subdomain with plain HTML prices.

Layout per product tile:
  <div id="line_<id>" data-talker>
    <a href="/lines/<slug>"> ... <div class="talker__name" title="Monster Aussie Lemonade 500mL">
      <span class="talker__product-name">Monster Aussie Lemonade</span>
      <span class="talker__name__size">500mL</span>
      <span class="talker__prices__was weak">was $4.49</span>
      <strong class="price__sell">$3.29</strong>
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from selectolax.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Listing, Product, Store
from .base import HttpClient, ScrapeStats, active_stores, chain_by_code, record_price
from .normalize import match_product, parse_pack

log = logging.getLogger(__name__)

CHAIN_CODE = "spudshed"
CATEGORY_PATHS = ("/category/energy-drinks", "/category/energy-drink-multipacks")
WAS_RE = re.compile(r"was\s*\$?\s*(\d{1,3}(?:\.\d{2})?)", re.I)
MONEY_RE = re.compile(r"\$?\s*(\d{1,3}(?:\.\d{2})?)")


@dataclass
class SpudItem:
    line_id: str
    url: str
    title: str
    size_text: str | None
    price_cents: int
    was_price_cents: int | None


def _money(text: str | None) -> int | None:
    if not text:
        return None
    match = MONEY_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        return round(float(match.group(1)) * 100)
    except ValueError:
        return None


def store_base_url(store: Store) -> str | None:
    if store.website:
        return store.website.rstrip("/")
    ref = (store.external_ref or "").strip()
    if ref and "." in ref and ":" not in ref:
        return f"https://{ref}"
    return None


def parse_items(html: str, base_url: str) -> list[SpudItem]:
    tree = HTMLParser(html)
    items: list[SpudItem] = []
    for node in tree.css("div[data-talker]"):
        node_id = node.attributes.get("id") or ""
        if not node_id.startswith("line_"):
            continue
        link = node.css_first('a[href^="/lines/"]')
        name_node = node.css_first(".talker__name")
        price_node = node.css_first(".price__sell")
        if link is None or name_node is None or price_node is None:
            continue
        title = (name_node.attributes.get("title") or "").strip()
        if not title:
            inner = node.css_first(".talker__product-name")
            size = node.css_first(".talker__name__size")
            parts = []
            if inner is not None and inner.text(strip=True):
                parts.append(inner.text(strip=True))
            if size is not None and size.text(strip=True):
                parts.append(size.text(strip=True))
            title = " ".join(parts).strip()
        if not title:
            continue
        price_cents = _money(price_node.text(strip=True))
        if not price_cents:
            continue
        was_node = node.css_first(".talker__prices__was")
        was_cents = None
        if was_node is not None:
            found = WAS_RE.search(was_node.text(separator=" ", strip=True))
            if found:
                was_cents = round(float(found.group(1)) * 100)
        href = link.attributes.get("href") or ""
        size_node = node.css_first(".talker__name__size")
        items.append(
            SpudItem(
                line_id=node_id.removeprefix("line_"),
                url=base_url + href,
                title=title,
                size_text=size_node.text(strip=True) if size_node is not None else None,
                price_cents=price_cents,
                was_price_cents=was_cents if was_cents and was_cents > price_cents else None,
            )
        )
    return items


def _upsert_listing(
    session: Session, chain_id: int, item: SpudItem, product: Product, pack_count: int
) -> Listing:
    listing = session.scalar(
        select(Listing).where(Listing.chain_id == chain_id, Listing.external_sku == item.line_id)
    )
    if listing is None:
        listing = Listing(chain_id=chain_id, external_sku=item.line_id)
        session.add(listing)
    listing.product_id = product.id
    listing.url = item.url
    listing.title_raw = item.title
    listing.size_text = item.size_text
    listing.pack_count = pack_count
    listing.last_seen_at = datetime.now(timezone.utc)
    session.flush()
    return listing


def scrape_store(
    client: HttpClient, session: Session, chain_id: int, store: Store, stats: ScrapeStats
) -> None:
    base_url = store_base_url(store)
    if not base_url:
        stats.errors.append(f"store {store.id} has no spudshed subdomain/website")
        return
    seen: set[str] = set()
    for path in CATEGORY_PATHS:
        try:
            response = client.get(base_url + path)
        except Exception as exc:
            stats.errors.append(f"{store.name}: {path} -> {exc}")
            continue
        for item in parse_items(response.text, base_url):
            if item.line_id in seen:
                continue
            seen.add(item.line_id)
            stats.items_seen += 1
            size_ml, pack_count = parse_pack(item.title)
            product, created = match_product(
                session,
                name=item.title,
                size_ml=size_ml,
                pack_count=pack_count,
                category="energy",
            )
            if created:
                stats.products_created += 1
            if product is None:
                continue
            listing = _upsert_listing(session, chain_id, item, product, pack_count)
            promo_kind = "special" if item.was_price_cents else "none"
            written = record_price(
                session,
                product=product,
                chain_id=chain_id,
                store_id=store.id,
                price_cents=item.price_cents,
                was_price_cents=item.was_price_cents,
                promo_kind=promo_kind,
                listing_id=listing.id,
                price_scope="store",
            )
            if written:
                stats.observations_written += 1
    session.commit()


def scrape(
    session: Session,
    store_ids: list[int] | None = None,
    stats: ScrapeStats | None = None,
) -> ScrapeStats:
    stats = stats or ScrapeStats(chain=CHAIN_CODE)
    chain = chain_by_code(session, CHAIN_CODE)
    if chain is None:
        stats.errors.append("chain 'spudshed' not seeded")
        return stats
    stores = active_stores(session, chain.id)
    if store_ids:
        stores = [store for store in stores if store.id in set(store_ids)]
    with HttpClient(min_interval=1.5) as client:
        for store in stores:
            if not store_base_url(store):
                continue
            before = stats.observations_written
            scrape_store(client, session, chain.id, store, stats)
            log.info(
                "spudshed %s: +%s prices (%s items seen so far)",
                store.name,
                stats.observations_written - before,
                stats.items_seen,
            )
    return stats
