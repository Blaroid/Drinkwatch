"""Fill the barcode index from the product-detail pages that publish a GTIN.

Woolworths and Coles both leave ``Barcode``/GTIN out of their search payloads
but include it on the product page; SpudShed publishes no GTIN at all. This
script walks the existing listings that have no code, asks the owning chain once
per SKU, and indexes whatever comes back. Dry run by default because it makes
real requests (rate-limited) and can write listings/products/barcodes:

    python scripts/backfill_barcodes.py                  # show what would change
    python scripts/backfill_barcodes.py --apply          # write it
    python scripts/backfill_barcodes.py --chain coles --limit 20 --apply
    python scripts/backfill_barcodes.py --refresh --apply  # re-ask past misses

The scrapers use the same lookup path with a small per-run budget
(``BARCODE_LOOKUP_LIMIT``), so a normal daily run keeps chipping away at the gap
without this script.
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, ".")

from sqlalchemy import or_, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import Chain, Listing, Product  # noqa: E402
from app.scrape import coles, woolworths  # noqa: E402
from app.scrape.base import CffiClient, HttpClient  # noqa: E402
from app.scrape.gtin import enrich_listing  # noqa: E402
from app.scrape.normalize import register_barcode  # noqa: E402

log = logging.getLogger("backfill_barcodes")

SUPPORTED = ("woolworths", "coles")


def _uncoded_listings(session, chain_id: int, limit: int | None):
    statement = (
        select(Listing, Product)
        .join(Product, Listing.product_id == Product.id)
        .where(
            Listing.chain_id == chain_id,
            Listing.barcode.is_(None),
            Product.barcode.is_(None),
            Product.normalised_barcode.is_(None),
            Product.is_active.is_(True),
        )
        .order_by(Listing.id)
    )
    if limit:
        statement = statement.limit(limit)
    return list(session.execute(statement).all())


def _backfill_chain(session, code: str, args, counts: dict[str, int]) -> None:
    chain = session.scalar(select(Chain).where(Chain.code == code))
    if chain is None:
        print(f"{code}: chain not seeded, skipped")
        return
    listings = _uncoded_listings(session, chain.id, args.limit)
    if not listings:
        print(f"{code}: no uncoded listings")
        return
    print(f"{code}: {len(listings)} uncoded listings to check")

    if code == "woolworths":
        client_ctx = HttpClient(browser=True, min_interval=args.interval)
    else:
        client_ctx = CffiClient(min_interval=args.interval)

    with client_ctx as client:
        if code == "woolworths":
            woolworths.prime_session(client)

            def fetch(sku: str) -> str | None:
                return woolworths.fetch_barcode(client, sku)

        else:

            def fetch(sku: str) -> str | None:
                return coles.fetch_barcode(client, sku)

        # One lookup budget per candidate; memoised answers do not spend it.
        budget = len(listings)
        for listing, product in listings:
            before = budget
            try:
                found, budget = enrich_listing(
                    session,
                    listing,
                    product,
                    chain=code,
                    source=f"{code}-detail",
                    budget=budget,
                    fetch=fetch,
                    refresh=args.refresh,
                )
            except Exception as exc:  # network/HTTP: leave it for the next run
                counts["failed"] += 1
                print(f"{code} {listing.external_sku}: lookup failed: {exc}")
                continue
            if budget < before:
                counts["asked"] += 1
            if found:
                counts["found"] += 1
                print(f"{code} {listing.external_sku}: {found}")
            elif budget < before:
                counts["no_code"] += 1
                print(f"{code} {listing.external_sku}: no GTIN published")


def _reindex(session, apply_changes: bool) -> int:
    """Index every code already stored on a product or listing."""
    indexed = 0
    products = session.scalars(
        select(Product).where(
            or_(Product.barcode.is_not(None), Product.normalised_barcode.is_not(None))
        )
    )
    for product in products:
        code = product.barcode or product.normalised_barcode
        if code:
            if apply_changes:
                indexed += register_barcode(session, product.id, code, source="backfill")
            else:
                indexed += 1
    listings = session.scalars(select(Listing).where(Listing.barcode.is_not(None)))
    for listing in listings:
        if listing.barcode and listing.product_id:
            if apply_changes:
                indexed += register_barcode(
                    session, listing.product_id, listing.barcode, source="backfill"
                )
            else:
                indexed += 1
    return indexed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--chain",
        action="append",
        choices=SUPPORTED,
        dest="chains",
        help=f"chain code (repeatable; default: {' '.join(SUPPORTED)})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum listings to check per chain; 0 means no limit (default: 100)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="minimum seconds between requests (default: 2.0)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore the lookup memo and re-ask SKUs checked in the last 30 days",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the codes and index rows; without it the transaction is rolled back",
    )
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be >= 0")
    args.limit = args.limit or None

    counts = {"asked": 0, "found": 0, "no_code": 0, "failed": 0}
    print(
        f"barcode lookup limit is {settings.barcode_lookup_limit} per scrape run; "
        f"this backfill asks at most {args.limit or 'all'} listings per chain"
    )
    with session_scope() as session:
        for code in args.chains or SUPPORTED:
            _backfill_chain(session, code, args, counts)
        indexed = _reindex(session, args.apply)
        print(
            f"asked {counts['asked']} SKUs, found {counts['found']} codes, "
            f"{counts['no_code']} with no published GTIN, {counts['failed']} failed; "
            f"{'indexed' if args.apply else 'would index'} {indexed} stored codes"
        )
        if args.apply:
            print("applied")
            return 0
        session.rollback()
    print("dry run: rolled back (pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
