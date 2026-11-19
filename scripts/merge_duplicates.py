"""Merge the duplicate plain-SKU rows the brand-only matching bug created.

A retailer title like "Red Bull Energy Drink 473mL" names no flavour, so
``match_product()`` scored it against every candidate as 0.0 and created a second
row ("red-bull-original-473ml") next to the one it already had. Each chain then
scraped into whichever row it had made first, so the two rows split the prices
for one can. The matching bug is fixed in ``app/scrape/normalize.py``; this folds
the pairs that already exist.

The older row survives: its slug is the one clients have looked up longest.
References move across, then the newer row is deactivated rather than deleted so
its id stays resolvable.

    python scripts/merge_duplicates.py          # report only, nothing written
    python scripts/merge_duplicates.py --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

sys.path.insert(0, ".")

from app.db import session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Barcode,
    Listing,
    Observation,
    PriceAlert,
    PriceDaily,
    Product,
)
from app.scrape.normalize import normalize_barcode  # noqa: E402


@dataclass(frozen=True)
class Pair:
    survivor: Product
    loser: Product


def _is_plain(product: Product) -> bool:
    """Only the base variant: the bug never produced flavoured twins."""
    return not product.variant and product.name.strip().lower() == "original"


def plain_duplicate_pairs(session: Session) -> list[Pair]:
    """Live plain-SKU rows that share a brand/size/pack key.

    Rows already linked into a canonical group and rows that are inactive are
    left alone: both are curation, not the scraper making its own duplicate.
    """
    buckets: dict[tuple[str, int | None, int], list[Product]] = {}
    for product in session.scalars(select(Product)):
        if not product.is_active or product.canonical_product_id is not None:
            continue
        if not _is_plain(product):
            continue
        key = (product.brand.strip().lower(), product.size_ml, product.pack_count)
        buckets.setdefault(key, []).append(product)

    pairs: list[Pair] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda product: product.id)
        survivor = members[0]
        for loser in members[1:]:
            pairs.append(Pair(survivor, loser))
    pairs.sort(key=lambda pair: pair.loser.id)
    return pairs


def _move_price_daily(session: Session, survivor_id: int, loser_id: int) -> int:
    """Repoint daily history, dropping days the survivor already has.

    price_daily is keyed by (day, chain, product, store) and the two rows were
    scraped on the same days, so a straight update would collide. The survivor's
    row wins: it is the price the surviving slug has been serving. Nothing here
    loads PriceDaily objects - the primary key has no store in it, so the ORM
    cannot tell two stores' rows apart - which is why the writes name each row
    by its full key instead.
    """
    keys = (PriceDaily.day, PriceDaily.chain_id, PriceDaily.store_id)
    taken = {
        tuple(row)
        for row in session.execute(select(*keys).where(PriceDaily.product_id == survivor_id))
    }
    moved = 0
    for row in session.execute(select(*keys).where(PriceDaily.product_id == loser_id)).all():
        key = tuple(row)
        matched = (
            PriceDaily.day == row.day,
            PriceDaily.chain_id == row.chain_id,
            PriceDaily.product_id == loser_id,
            # store_id is nullable, so "the same store" needs is_(None), not ==.
            PriceDaily.store_id.is_(None)
            if row.store_id is None
            else PriceDaily.store_id == row.store_id,
        )
        if key in taken:
            session.execute(
                delete(PriceDaily).where(*matched).execution_options(synchronize_session=False)
            )
        else:
            session.execute(
                update(PriceDaily)
                .where(*matched)
                .values(product_id=survivor_id)
                .execution_options(synchronize_session=False)
            )
            taken.add(key)
            moved += 1
    return moved


def merge_pair(session: Session, pair: Pair) -> dict[str, int]:
    """Move every reference from the loser to the survivor.

    Returns how many rows of each kind moved, for the dry-run report.
    """
    survivor, loser = pair.survivor, pair.loser
    moved = {
        "listings": session.execute(
            update(Listing)
            .where(Listing.product_id == loser.id)
            .values(product_id=survivor.id)
        ).rowcount,
        "observations": session.execute(
            update(Observation)
            .where(Observation.product_id == loser.id)
            .values(product_id=survivor.id)
        ).rowcount,
        "barcodes": session.execute(
            update(Barcode)
            .where(Barcode.product_id == loser.id)
            .values(product_id=survivor.id)
        ).rowcount,
        # An alert stores the product it watches: left on the loser it would
        # keep firing against a product whose prices have just moved away.
        "alerts": session.execute(
            update(PriceAlert)
            .where(PriceAlert.product_id == loser.id)
            .values(product_id=survivor.id)
        ).rowcount,
        # Rows linked to the loser as their canonical group.
        "aliases": session.execute(
            update(Product)
            .where(Product.canonical_product_id == loser.id)
            .values(canonical_product_id=survivor.id)
        ).rowcount,
    }
    moved["price_daily"] = _move_price_daily(session, survivor.id, loser.id)

    # products.barcode is unique, so hand the code to the survivor before the
    # loser lets go of it: the flush in between keeps the two updates apart.
    if survivor.barcode is None and loser.barcode:
        code, normalised = loser.barcode, loser.normalised_barcode
        loser.barcode = None
        loser.normalised_barcode = None
        session.flush()
        survivor.barcode = code
        survivor.normalised_barcode = normalised or normalize_barcode(code)

    loser.is_active = False
    return moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge duplicate plain-SKU product rows")
    parser.add_argument("--apply", action="store_true", help="write the merges (default: report)")
    args = parser.parse_args(argv)

    with session_scope() as session:
        pairs = plain_duplicate_pairs(session)
        if not pairs:
            print("no duplicate plain-SKU rows")
            return 0
        for pair in pairs:
            moved = merge_pair(session, pair)
            detail = ", ".join(f"{kind} {count}" for kind, count in moved.items() if count)
            print(f"{pair.survivor.slug} <- {pair.loser.slug}: {detail or 'no references'}")
        print(
            ("merged " if args.apply else "would merge ")
            + f"{len(pairs)} duplicate pair(s); the older slug of each pair survives"
        )
        if not args.apply:
            # Report only: undo the work, as scripts/clean_names.py does.
            session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
