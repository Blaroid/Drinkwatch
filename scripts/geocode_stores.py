"""Fill in store suburb/postcode and human-readable names.

OpenStreetMap store nodes often carry only the brand ("Woolworths") with no
address tags. One Overpass query fetches every suburb/town node in the metro
area, then each store is matched to its nearest one locally (no bulk Nominatim).

    python scripts/geocode_stores.py [--dry-run] [--max-km 6]
"""

from __future__ import annotations

import argparse
import math
import sys

import httpx
from sqlalchemy import select

sys.path.insert(0, ".")

from app.db import session_scope  # noqa: E402
from app.models import Chain, Store  # noqa: E402

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
BBOX = (-32.75, 115.55, -31.45, 116.25)  # south, west, north, east
UA = "drinkwatch-seed/0.1 (store suburb lookup)"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def fetch_places() -> list[tuple[str, float, float, str | None]]:
    south, west, north, east = BBOX
    query = f"""
[out:json][timeout:180];
(
  node["place"~"^(suburb|town|village|neighbourhood)$"]({south},{west},{north},{east});
);
out tags center;
"""
    with httpx.Client(timeout=240.0, headers={"User-Agent": UA}) as client:
        response = client.post(OVERPASS_URL, data={"data": query})
        response.raise_for_status()
        elements = response.json().get("elements", [])
    places = []
    for element in elements:
        tags = element.get("tags") or {}
        name = tags.get("name")
        lat = element.get("lat")
        lon = element.get("lon")
        if name and lat is not None and lon is not None:
            places.append((name, float(lat), float(lon), tags.get("postcode")))
    print(f"fetched {len(places)} place nodes")
    return places


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assign suburbs to stores")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-km", type=float, default=6.0)
    args = parser.parse_args(argv)

    places = fetch_places()
    if not places:
        print("no places found; aborting")
        return 1

    with session_scope() as session:
        chains = {chain.id: chain for chain in session.scalars(select(Chain))}
        stores = list(session.scalars(select(Store).where(Store.is_active.is_(True))))
        updated = 0
        for store in stores:
            if store.lat is None or store.lon is None:
                continue
            if store.suburb:
                continue
            best: tuple[float, str, str | None] | None = None
            for name, lat, lon, postcode in places:
                distance = haversine_km(store.lat, store.lon, lat, lon)
                if best is None or distance < best[0]:
                    best = (distance, name, postcode)
            if best is None or best[0] > args.max_km:
                continue
            chain = chains.get(store.chain_id)
            store.suburb = best[1]
            if best[2]:
                store.postcode = best[2]
            bare_names = {chain.name.lower(), chain.name.replace("-", "").lower()} if chain else set()
            if chain and store.name.strip().lower() in bare_names | {"woolworths", "coles", "iga", "spudshed", "bp", "ampol", "vibe", "7-eleven"}:
                store.name = f"{chain.name} {best[1]}"
            updated += 1
        print(f"updated {updated} stores")
        if args.dry_run:
            session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
