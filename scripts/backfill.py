"""Backfill derived product columns: normalised barcode, family slug, caffeine.

Run after schema changes that add derived columns, or after a fresh scrape.

    python scripts/backfill.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from app.caffeine import apply_reference_caffeine  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import Listing, Product  # noqa: E402
from app.scrape.normalize import (  # noqa: E402
    classify_category,
    family_slug_for,
    normalize_barcode,
    register_barcode,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill derived columns")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    with session_scope() as session:
        products = list(session.scalars(__import__("sqlalchemy").select(Product)))
        barcodes = families = 0
        for product in products:
            normalised = product.normalised_barcode or normalize_barcode(product.barcode)
            if normalised and normalised != product.normalised_barcode:
                product.normalised_barcode = normalised
                barcodes += 1
            if not product.family_slug:
                product.family_slug = family_slug_for(product.slug)
                families += 1
        listings = list(session.scalars(__import__("sqlalchemy").select(Listing)))
        listing_barcodes = 0
        for listing in listings:
            normalised = normalize_barcode(listing.barcode)
            if normalised and normalised != listing.barcode:
                listing.barcode = normalised
                listing_barcodes += 1
        recategorised = 0
        for product in products:
            wanted = classify_category(product.brand, product.name)
            if product.category != wanted and product.category == "energy":
                product.category = wanted
                recategorised += 1

        # Existing rows would otherwise stay on the FSANZ fallback until they
        # are touched again; this is the one-time pass that fills them.
        caffeine_filled = sum(
            1 for product in products if apply_reference_caffeine(session, product)
        )

        indexed = 0
        for product in products:
            source_code = product.barcode or product.normalised_barcode
            if source_code:
                indexed += register_barcode(session, product.id, source_code)
        for listing in listings:
            if listing.barcode and listing.product_id:
                indexed += register_barcode(session, listing.product_id, listing.barcode)

        print(
            f"products: {len(products)} rows, {barcodes} barcodes normalised, {families} families set; "
            f"listings: {len(listings)} rows, {listing_barcodes} barcodes normalised; "
            f"{recategorised} products recategorised, {indexed} barcode index rows written, "
            f"{caffeine_filled} caffeine values seeded"
        )
        if args.dry_run:
            session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
