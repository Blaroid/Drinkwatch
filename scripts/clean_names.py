"""Clean up product names that were stored as raw retailer titles.

Early scrapes stored things like "Red Bull Energy Drink Can 250mL" as the product
name, which the app then rendered as "Red Bull Red Bull Energy Drink Can 250ml
250ml". Names should be the flavour only, with "Original" for the plain variant.

Slugs are deliberately left alone: devices have tracked drinks by slug.

    python scripts/clean_names.py [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys

from sqlalchemy import select

sys.path.insert(0, ".")

from app.db import session_scope  # noqa: E402
from app.models import Listing, Product  # noqa: E402
from app.scrape.normalize import canonical_brand, clean_flavour, parse_pack  # noqa: E402

DIRTY = (
    re.compile(r"\benergy\s*drink\b", re.I),
    re.compile(r"\b\d+\s*(?:ml|l)\b", re.I),
    re.compile(r"\b\d+\s*x\s*\d", re.I),
    re.compile(r"\b(?:cans?|bottles?|pack|multipack)\b", re.I),
)


def looks_like_raw_title(name: str, brand: str) -> bool:
    """True only for names that still carry retailer boilerplate.

    A name like "Ultra Blue Hawaiian" is already good; rewriting it from another
    chain's listing would make it worse ("Blue Hawaiian").
    """
    if brand and brand.lower() in name.lower():
        return True
    return any(pattern.search(name) for pattern in DIRTY)


def preferred_title(product: Product, listings: list[Listing]) -> str | None:
    """Pick the listing whose size/pack matches the product, else any."""
    usable = [listing for listing in listings if listing.title_raw]
    if not usable:
        return None
    for listing in usable:
        size_ml, pack_count = parse_pack(listing.title_raw)
        if size_ml == product.size_ml and pack_count == product.pack_count:
            return listing.title_raw
    return usable[0].title_raw


def tidy(text: str) -> str:
    words = [word for word in text.split() if word]
    joined = " ".join(words).strip(" -")
    if not joined:
        return "Original"
    # Retailer titles are often SHOUTY or lowercase; normalise to Title Case.
    if joined.isupper() or joined.islower():
        return joined.title()
    return joined[0].upper() + joined[1:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean stored product names")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    changed = 0
    with session_scope() as session:
        listings_by_product: dict[int, list[Listing]] = {}
        for listing in session.scalars(select(Listing).where(Listing.product_id.is_not(None))):
            listings_by_product.setdefault(listing.product_id, []).append(listing)

        for product in session.scalars(select(Product)):
            brand = canonical_brand(product.brand)
            current = product.name.strip()
            if not looks_like_raw_title(current, brand):
                continue

            # Prefer cleaning the name we already have: it is the one users see and
            # it matches the size/pack we stored.
            flavour = tidy(clean_flavour(current, brand))
            if flavour.lower() in {"", "original"} or len(flavour) < 2:
                flavour = "Original"
            if flavour != product.name:
                if args.dry_run:
                    print(f"{product.slug}: {product.name!r} -> {flavour!r}")
                product.name = flavour
                changed += 1

        print(("would update " if args.dry_run else "updated ") + str(changed) + " product name(s)")
        if args.dry_run:
            session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
