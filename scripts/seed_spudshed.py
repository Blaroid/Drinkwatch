"""Seed the SpudShed stores from their own store chooser.

https://stores.spudshed.com.au/ is the authoritative list of SpudShed stores that
have an online shop, and each one is served from <suburb>.spudshed.com.au.
OpenStreetMap has stale/incorrect SpudShed entries, so those rows get deactivated.
The chooser is the only source of the live set, so a failed read must abort
instead of looking like every store closed at once.

    python scripts/seed_spudshed.py [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import unicodedata

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, ".")

from app.db import init_db, session_scope  # noqa: E402
from app.models import Chain, Store  # noqa: E402

CHOOSER_URL = "https://stores.spudshed.com.au/change"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
UA = "drinkwatch-seed/0.1 (spudshed store list)"

# The chooser lists seven stores. Zero or one card means the page was empty,
# served by JS, or its markup changed, not that SpudShed shrank to one shop;
# treating that as the live list would deactivate every store row.
MIN_CARDS = 2


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def parse_cards(html: str) -> list[dict]:
    cards: list[dict] = []
    for match in re.finditer(r'<div class="StoreCard[^"]*">(.*?)</div>\s*</div>\s*</div>', html, re.S):
        block = match.group(1)
        name = re.search(r'<span class="StoreCard__Name">([^<]+)</span>', block)
        details = re.search(r'<span class="StoreCard__Details">(.*?)</span>', block, re.S)
        if not name or not details:
            continue
        lines = [
            re.sub(r"<[^>]+>", "", part).strip()
            for part in re.split(r"<br\s*/?>", details.group(1))
        ]
        lines = [line for line in lines if line and "open" not in line.lower()]
        suburb_line = lines[-1] if lines else ""
        m = re.match(r"(.+?)\s+WA\s+(\d{4})$", suburb_line.strip())
        suburb, postcode = (m.group(1).strip(), m.group(2)) if m else (suburb_line.strip(), None)
        cards.append(
            {
                "name": name.group(1).strip(),
                "address": ", ".join(lines[:-1]) if len(lines) > 1 else "",
                "suburb": suburb,
                "postcode": postcode,
            }
        )
    # The chooser renders each card twice (mobile + desktop); collapse duplicates.
    unique: dict[str, dict] = {}
    for card in cards:
        unique.setdefault(card["suburb"].lower(), card)
    return list(unique.values())


def geocode(address: str, *fallbacks: str) -> tuple[float, float] | None:
    """Forward geocode with progressively looser queries (Nominatim, 2s spacing)."""
    queries = [q for q in (address, *fallbacks) if q]
    with httpx.Client(timeout=25.0, headers={"User-Agent": UA}) as client:
        for query in queries:
            params = {
                "q": query,
                "format": "jsonv2",
                "countrycodes": "au",
                "limit": 1,
                "addressdetails": 0,
            }
            try:
                response = client.get(NOMINATIM_URL, params=params)
                response.raise_for_status()
                results = response.json() or []
            except Exception as exc:  # pragma: no cover - network path
                print(f"    geocode error for {query}: {exc}")
                time.sleep(2.0)
                continue
            if results:
                return float(results[0]["lat"]), float(results[0]["lon"])
            time.sleep(2.0)
    return None


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def resolve_subdomain(name: str, suburb: str) -> str | None:
    """Find the online shop subdomain for a store.

    SpudShed uses the store name squashed together (bentleyplaza) but the newer
    stores use their suburb (kalgoorlie, waterford), so try both shapes.
    """
    candidates: list[str] = []
    for raw in (name, suburb):
        for candidate in (slugify(raw), _compact(raw)):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    with httpx.Client(timeout=25.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for candidate in candidates:
            url = f"https://{candidate}.spudshed.com.au/"
            try:
                response = client.get(url)
            except Exception:
                continue
            if response.status_code != 200:
                continue
            title = re.search(r"<title>([^<]*)</title>", response.text, re.I)
            if title is None:
                continue
            title_text = title.group(1).lower()
            if "spudshed" not in title_text:
                continue
            label = title_text.replace("spudshed", "").strip()
            if _compact(label) == _compact(name) or _compact(label) == _compact(suburb):
                return candidate
    return None


def sync_stores(session: Session, chain: Chain, cards: list[dict]) -> int:
    """Upsert the parsed cards and retire rows the chooser no longer lists.

    Returns the number of rows deactivated. A card whose subdomain probe failed
    leaves its store out of the live set, which would make the row look closed,
    so the retire pass is skipped unless every card resolved: a network error
    must not retire a shop. Rows absent from a fully resolved list are genuinely
    gone and keep the old deactivation behaviour.
    """
    kept: set[str] = set()
    unresolved: list[str] = []
    for card in cards:
        slug = card["slug"]
        if not slug:
            unresolved.append(card["name"])
            print(f"  skipping {card['name']}: no online shop found")
            continue
        ref = f"spudshed:{slug}"
        kept.add(ref)
        store = session.scalar(
            select(Store).where(Store.chain_id == chain.id, Store.external_ref == ref)
        )
        if store is None:
            store = Store(chain_id=chain.id, external_ref=ref)
            session.add(store)
        store.name = f"SpudShed {card['name']}"
        store.suburb = card["suburb"]
        store.state = "WA"
        store.postcode = card["postcode"]
        if card["coords"]:
            store.lat, store.lon = card["coords"]
        store.website = f"https://{slug}.spudshed.com.au"
        store.is_active = True
        print(f"  upserted {store.name} ({'active' if store.is_active else 'inactive'})")

    if unresolved:
        print(
            f"  {len(unresolved)} card(s) without an online shop; "
            "not retiring any row because the live list is incomplete"
        )
        return 0

    deactivated = 0
    for store in list(session.scalars(select(Store).where(Store.chain_id == chain.id))):
        ref = store.external_ref or ""
        if ref in kept or not store.is_active:
            continue
        store.is_active = False
        deactivated += 1
        print(f"  deactivated stale row {store.name} ({store.suburb}) ref={ref}")
    print(f"  deactivated {deactivated} stale rows")
    return deactivated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed SpudShed stores")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    with httpx.Client(timeout=40.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        html = client.get(CHOOSER_URL).text
    cards = parse_cards(html)
    print(f"found {len(cards)} online stores: {[card['suburb'] for card in cards]}")
    if len(cards) < MIN_CARDS:
        print(f"  only {len(cards)} card(s) parsed; aborting before touching store rows")
        return 1

    init_db()

    for card in cards:
        card["slug"] = resolve_subdomain(card["name"], card["suburb"])
        street = ", ".join(filter(None, [card["address"], card["suburb"] + " WA", card["postcode"]]))
        suburb_only = f"{card['suburb']} WA {card['postcode'] or ''}".strip()
        card["coords"] = geocode(
            street,
            suburb_only,
            card["name"] + " " + suburb_only,
        )
        time.sleep(2.0)
        print(
            f"  {card['name']:<14} {card['suburb']:<11} slug={str(card['slug']):<14} coords={card['coords']}"
        )

    if args.dry_run:
        return 0

    with session_scope() as session:
        chain = session.scalar(select(Chain).where(Chain.code == "spudshed"))
        if chain is None:
            chain = Chain(code="spudshed", name="SpudShed", kind="supermarket", scrape_enabled=True)
            session.add(chain)
            session.flush()

        sync_stores(session, chain, cards)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
