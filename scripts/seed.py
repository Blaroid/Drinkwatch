"""Seed chains and Perth stores.

Store locations come from OpenStreetMap (Overpass API) so we are not scraping
retailer store locators. SpudShed stores also get their per-store online shop
subdomain discovered, which is what the SpudShed scraper needs.

Usage (inside the api container):
    python scripts/seed.py                 # chains + Perth stores
    python scripts/seed.py --chains-only
    python scripts/seed.py --bbox=-32.75,115.55,-31.45,116.25
    python scripts/seed.py --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import unicodedata

import httpx
from sqlalchemy import select

sys.path.insert(0, ".")

from app.db import init_db, session_scope  # noqa: E402
from app.models import Chain, Store  # noqa: E402

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_BBOX = (-32.75, 115.55, -31.45, 116.25)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

CHAINS = [
    # code, display name, kind, scrape_enabled
    ("woolworths", "Woolworths", "supermarket", True),
    ("coles", "Coles", "supermarket", True),
    ("spudshed", "SpudShed", "supermarket", True),
    ("iga", "IGA", "supermarket", False),
    ("7eleven", "7-Eleven", "convenience", False),
    ("vibe", "Vibe", "convenience", False),
    ("ampol", "Ampol", "servo", False),
    ("bp", "BP", "servo", False),
]

# (chain code, regex on brand+name)
BRAND_RULES = [
    ("woolworths", re.compile(r"\bwoolworths\b|\bsafeway\b", re.I)),
    ("coles", re.compile(r"\bcoles\b(?!\s*express)", re.I)),
    ("spudshed", re.compile(r"\bspuds?hed\b", re.I)),
    ("iga", re.compile(r"\biga\b|\biga\s*x[- ]?press\b", re.I)),
    ("7eleven", re.compile(r"\b7[\s-]?eleven\b", re.I)),
    ("vibe", re.compile(r"\bvibe\b", re.I)),
    ("ampol", re.compile(r"\bampol\b|\bcaltex\b", re.I)),
    ("bp", re.compile(r"^bp\b|\bbp\s*(connect|truckstop|service)", re.I)),
]


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def classify(tags: dict) -> str | None:
    brand = tags.get("brand") or tags.get("operator") or ""
    name = tags.get("name") or ""
    haystack = f"{brand} {name}".strip()
    if not haystack:
        return None
    for code, pattern in BRAND_RULES:
        if pattern.search(haystack):
            return code
    return None


def fetch_osm(bbox: tuple[float, float, float, float]) -> list[dict]:
    south, west, north, east = bbox
    query = f"""
[out:json][timeout:180];
(
  node["name"]({south},{west},{north},{east});
  way["name"]({south},{west},{north},{east});
);
out center tags;
"""
    print(f"querying overpass for bbox {bbox} ...")
    with httpx.Client(timeout=240.0, headers={"User-Agent": "drinkwatch-seed/0.1"}) as client:
        response = client.post(OVERPASS_URL, data={"data": query})
        response.raise_for_status()
        payload = response.json()
    return payload.get("elements", [])


def extract(elements: list[dict]) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for element in elements:
        tags = element.get("tags") or {}
        if not tags:
            continue
        amenity = tags.get("amenity")
        shop = tags.get("shop")
        if shop not in {"supermarket", "convenience", "greengrocer"} and amenity != "fuel":
            continue
        code = classify(tags)
        if code is None:
            continue
        lat = element.get("lat") or (element.get("center") or {}).get("lat")
        lon = element.get("lon") or (element.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        key = (code, round(lat * 200), round(lon * 200))
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "chain_code": code,
                "name": tags.get("name") or tags.get("brand") or code,
                "suburb": tags.get("addr:suburb"),
                "postcode": tags.get("addr:postcode"),
                "state": tags.get("addr:state") or "WA",
                "lat": float(lat),
                "lon": float(lon),
                "external_ref": f"osm:{element.get('type')}/{element.get('id')}",
                "website": tags.get("website") or tags.get("contact:website"),
            }
        )
    return found


def discover_spudshed_subdomain(name: str, suburb: str | None) -> str | None:
    candidates: list[str] = []
    for raw in (suburb, re.sub(r"(?i)^spuds?hed\s*", "", name)):
        if not raw:
            continue
        slug = slugify(raw)
        if slug and slug not in candidates:
            candidates.append(slug)
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "drinkwatch-seed/0.1"}) as client:
        for slug in candidates:
            url = f"https://{slug}.spudshed.com.au/"
            try:
                response = client.get(url)
            except Exception:
                continue
            body = response.text
            if response.status_code != 200:
                continue
            title = re.search(r"<title>([^<]*)</title>", body, re.I)
            looks_like_store = (
                title is not None
                and "spudshed" in title.group(1).lower()
                and "talker" in body[:400000]
            )
            if looks_like_store:
                return slug
            time.sleep(0.5)
    return None


def reverse_suburb(lat: float, lon: float) -> str | None:
    """Nearest suburb name via Nominatim (1 request/second, real UA)."""
    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "zoom": 16,
        "addressdetails": 1,
    }
    try:
        with httpx.Client(timeout=20.0, headers={"User-Agent": "drinkwatch-seed/0.1 (store suburb lookup)"}) as client:
            response = client.get(NOMINATIM_URL, params=params)
            response.raise_for_status()
            address = (response.json() or {}).get("address", {}) or {}
    except Exception as exc:  # pragma: no cover - network path
        print(f"    reverse geocode failed for {lat},{lon}: {exc}")
        return None
    for key in ("suburb", "neighbourhood", "quarter", "city_district", "town", "village", "city"):
        if address.get(key):
            return str(address[key])
    return None


def upsert_chain(session, code: str, name: str, kind: str, scrape_enabled: bool) -> Chain:
    chain = session.scalar(select(Chain).where(Chain.code == code))
    if chain is None:
        chain = Chain(code=code, name=name, kind=kind, scrape_enabled=scrape_enabled)
        session.add(chain)
        session.flush()
        print(f"  + chain {code}")
    else:
        chain.name = name
        chain.kind = kind
        chain.scrape_enabled = scrape_enabled
    return chain


def upsert_store(session, chain: Chain, row: dict, dry_run: bool) -> bool:
    store = session.scalar(
        select(Store).where(Store.chain_id == chain.id, Store.external_ref == row["external_ref"])
    )
    if store is None:
        store = Store(chain_id=chain.id, external_ref=row["external_ref"])
        session.add(store)
    store.name = row["name"]
    store.suburb = row["suburb"]
    store.state = row["state"]
    store.postcode = row["postcode"]
    store.lat = row["lat"]
    store.lon = row["lon"]
    if row.get("website"):
        store.website = row["website"]
    store.is_active = True
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed chains and stores")
    parser.add_argument("--bbox", default=",".join(str(v) for v in DEFAULT_BBOX))
    parser.add_argument("--chains-only", action="store_true")
    parser.add_argument("--no-probe", action="store_true", help="skip SpudShed subdomain discovery")
    parser.add_argument("--only-chain", default=None, help="limit seeding to one chain code")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    bbox = tuple(float(part) for part in args.bbox.split(","))
    if len(bbox) != 4:
        parser.error("--bbox needs south,west,north,east")

    init_db()

    with session_scope() as session:
        print("chains:")
        chains = {
            code: upsert_chain(session, code, name, kind, enabled)
            for code, name, kind, enabled in CHAINS
        }
        if args.chains_only:
            return 0

    rows = extract(fetch_osm(bbox))
    if args.only_chain:
        rows = [row for row in rows if row["chain_code"] == args.only_chain]
    print(f"found {len(rows)} candidate stores")
    per_chain: dict[str, int] = {}
    spudshed_rows = [row for row in rows if row["chain_code"] == "spudshed"]

    if not args.no_probe:
        claimed: dict[str, str] = {}
        for row in spudshed_rows:
            if not row["suburb"]:
                row["suburb"] = reverse_suburb(row["lat"], row["lon"])
                time.sleep(1.1)
            if not row["suburb"]:
                print(f"  spudshed {row['name']} ({row['lat']:.4f},{row['lon']:.4f}): unknown suburb")
                continue
            slug = discover_spudshed_subdomain(row["name"], row["suburb"])
            if not slug:
                print(f"  spudshed {row['suburb']}: no online store")
                continue
            if slug in claimed:
                print(f"  spudshed {row['suburb']}: duplicate of {claimed[slug]}, skipping")
                continue
            claimed[slug] = row["external_ref"]
            row["external_ref"] = f"spudshed:{slug}"
            row["website"] = f"https://{slug}.spudshed.com.au"
            row["name"] = f"SpudShed {row['suburb']}"
            print(f"  spudshed {row['suburb']}: online store '{slug}'")

    if args.dry_run:
        for row in rows:
            per_chain[row["chain_code"]] = per_chain.get(row["chain_code"], 0) + 1
        print("dry run:", per_chain)
        return 0

    with session_scope() as session:
        chains = {chain.code: chain for chain in session.scalars(select(Chain))}
        for row in rows:
            chain = chains[row["chain_code"]]
            upsert_store(session, chain, row, args.dry_run)
            per_chain[row["chain_code"]] = per_chain.get(row["chain_code"], 0) + 1
    print("seeded:", per_chain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
