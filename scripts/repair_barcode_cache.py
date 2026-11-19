"""Clear Open Food Facts cache rows that pin a barcode to an answer nobody checked.

Two bugs left rows in ``barcode_cache`` that should never have been written. A
429 or a 500 from Open Food Facts was cached as "not found", so one bad hour
pinned a barcode as unknown for the life of the database. And nothing ever
expired a row, so ``OFF_TTL`` was dead code and a negative stayed wrong even
after Open Food Facts imported the product.

``app/api/barcode.py`` no longer writes a cache row for a non-200 answer and
honours the TTL on read. This script clears the rows the old behaviour left
behind: a purged barcode is simply looked up again, which is why deleting the
row is safe rather than rewriting it.

It also reports crowd bindings whose code has no valid GTIN check digit. Those
are the codes a misread could have added before the submission flow started
refusing them; nothing is changed here, because only a human can say whether the
code is a retailer invention or a typo. Use the admin API to repair one:

    POST   /v1/admin/barcodes/{code}/map   {"product_slug": "..."}
    DELETE /v1/admin/barcodes/{code}

    python scripts/repair_barcode_cache.py                 # dry run
    python scripts/repair_barcode_cache.py --apply
    python scripts/repair_barcode_cache.py --report-bindings
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import select

sys.path.insert(0, ".")

from app.api.barcode import OFF_TTL  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import Barcode, BarcodeCache, Product  # noqa: E402
from app.scrape.normalize import barcode_variants, ean13_checksum_ok  # noqa: E402


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _why(row: BarcodeCache, now: datetime) -> str | None:
    """The reason this row must go, or None when it is still trustworthy."""
    if not (row.payload or {}).get("found"):
        return "negative"
    if row.created_at is None or now - _as_utc(row.created_at) >= OFF_TTL:
        return "stale"
    return None


def _report_bindings(session) -> None:
    products = {product.id: product for product in session.scalars(select(Product))}
    rows = session.scalars(select(Barcode).where(Barcode.source == "crowd"))
    for row in rows:
        if any(ean13_checksum_ok(form) for form in barcode_variants(row.barcode)):
            continue
        product = products.get(row.product_id)
        slug = product.slug if product is not None else "?"
        print(
            f"{row.barcode} -> {slug} ({row.source}): no valid check digit; "
            "re-map with POST /v1/admin/barcodes/{code}/map or delete it"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge stale and failed barcode cache rows")
    parser.add_argument("--apply", action="store_true", help="delete the rows (default: dry run)")
    parser.add_argument(
        "--report-bindings",
        action="store_true",
        help="also list crowd barcode bindings with no valid check digit",
    )
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        purge: list[tuple[BarcodeCache, str]] = []
        for row in session.scalars(select(BarcodeCache).order_by(BarcodeCache.barcode)):
            reason = _why(row, now)
            if reason is None:
                continue
            found = (row.payload or {}).get("found")
            created = row.created_at.isoformat() if row.created_at else "unknown"
            print(f"{row.barcode}: {reason} (found={found}, source={row.source}, cached {created})")
            purge.append((row, reason))

        if args.apply:
            for row, _ in purge:
                session.delete(row)

        if args.report_bindings:
            _report_bindings(session)

        mode = "applied" if args.apply else "dry run"
        print(f"{mode}: {len(purge)} cache row(s) {'purged' if args.apply else 'to purge'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
