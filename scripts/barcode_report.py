"""Measure how much of the catalogue a scan can actually resolve.

Prints the products that have a code, the listings that carry one, the
per-chain gap, and the products still missing a GTIN. Read-only: it is the
before/after check for ``scripts/backfill_barcodes.py``.

    python scripts/barcode_report.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from sqlalchemy import func, or_, select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Barcode, Chain, Listing, Product  # noqa: E402


def _coded() -> tuple:
    return (Product.barcode.is_not(None), Product.normalised_barcode.is_not(None))


def main(argv: list[str] | None = None) -> int:
    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(Product)) or 0
        active = (
            session.scalar(
                select(func.count())
                .select_from(Product)
                .where(Product.is_active.is_(True), Product.status == "verified")
            )
            or 0
        )
        with_code = (
            session.scalar(
                select(func.count())
                .select_from(Product)
                .where(Product.is_active.is_(True), Product.status == "verified", or_(*_coded()))
            )
            or 0
        )
        indexed = (
            session.scalar(select(func.count(func.distinct(Barcode.product_id))))
            or 0
        )
        index_forms = session.scalar(select(func.count()).select_from(Barcode)) or 0

        print(f"products: {total} rows, {active} active+verified")
        print(f"  with a code: {with_code}/{active}")
        print(f"  reachable through the index: {indexed} products, {index_forms} code forms")

        per_chain = session.execute(
            select(
                Chain.code,
                func.count(Listing.id),
                func.count(Listing.barcode),
            )
            .select_from(Chain)
            .outerjoin(Listing, Listing.chain_id == Chain.id)
            .group_by(Chain.code)
            .order_by(Chain.code)
        ).all()
        print("\nlistings by chain (with a retailer code):")
        for code, listings, coded in per_chain:
            print(f"  {code:<12} {coded or 0:>4}/{listings or 0:<4}")

        gaps = session.execute(
            select(Chain.code, func.count(Listing.id))
            .join(Listing, Listing.chain_id == Chain.id)
            .join(Product, Listing.product_id == Product.id)
            .where(
                Listing.barcode.is_(None),
                Product.barcode.is_(None),
                Product.normalised_barcode.is_(None),
                Product.is_active.is_(True),
            )
            .group_by(Chain.code)
            .order_by(Chain.code)
        ).all()
        print("\nuncoded listings the chains could still answer for:")
        for code, count in gaps:
            if code in {"woolworths", "coles"}:
                print(f"  {code:<12} {count}")
            else:
                print(f"  {code:<12} {count} (no published GTIN)")

        missing = session.execute(
            select(Product.brand, func.count(Product.id))
            .where(Product.is_active.is_(True), Product.status == "verified", ~or_(*_coded()))
            .group_by(Product.brand)
            .order_by(func.count(Product.id).desc())
            .limit(10)
        ).all()
        print("\nactive+verified products without a code, by brand:")
        if not missing:
            print("  none")
        for brand, count in missing:
            print(f"  {brand:<20} {count}")

        unattached = (
            session.scalar(
                select(func.count())
                .select_from(Listing)
                .where(Listing.product_id.is_(None))
            )
            or 0
        )
        if unattached:
            print(f"\nlistings with no product to attach a code to: {unattached}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
