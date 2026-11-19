"""Re-derive the pack count of listings that were scraped as a single can.

``parse_pack()`` could not read the carton forms the chains publish - "500ml x
12", "(4 Pack) 250mL 24 Pack" - and took the carton volume Woolworths puts in
PackageSize ("6000ML" for 24 x 250mL) for a can size. A carton was therefore
stored as one can at the carton price: /v1/prices read Rockstar Guava Punch's
12-pack as "$60.95 / 500 mL can" (12190 cents a litre), and a phantom
``v-blue-6000ml`` product existed next to the real 250 mL cans.

The parser is fixed in app/scrape/normalize.py; this repairs the rows it already
wrote. Each listing is re-read from its stored title and size text, moved to the
product that pack describes (created or matched by ``match_product()``, so the
slug follows the same convention as a fresh scrape), and its observations are
repriced for the new pack. Price history (price_daily) moves with them through
the ledger writers in app/daily.py rather than by copying rows.

A product row shared with other listings is left alone and reported: its pack is
whatever those listings were sold in, so this script has no business rewriting
it. A product left with no listing and no observation is deactivated rather than
deleted, so a slug a device still tracks keeps resolving.

    python scripts/repair_pack_counts.py          # report only, nothing written
    python scripts/repair_pack_counts.py --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

sys.path.insert(0, ".")

from app.daily import sync_daily_price, withdraw_daily_price  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Barcode,
    Listing,
    Observation,
    PriceAlert,
    PriceDaily,
    Product,
)
from app.scrape.normalize import (  # noqa: E402
    match_product,
    normalize_barcode,
    parse_pack,
    per_litre_cents,
)


@dataclass(frozen=True)
class Fix:
    """One listing whose stored pack disagrees with the pack it describes."""

    listing: Listing
    size_ml: int
    pack_count: int


def listing_pack(listing: Listing) -> tuple[int | None, int]:
    """The pack a listing describes, read from the fields the scraper filled in.

    Both are needed: the can size is usually only in the title, while the carton
    quantity may be in either ("24 Pack" in the title, "x 12" in PackageSize).
    """
    return parse_pack(" ".join(filter(None, [listing.title_raw, listing.size_text])))


def _other_listings(session: Session, listing: Listing) -> int:
    """How many other listings share this listing's product."""
    return session.scalar(
        select(func.count())
        .select_from(Listing)
        .where(Listing.product_id == listing.product_id, Listing.id != listing.id)
    ) or 0


def pending_fixes(session: Session) -> tuple[list[Fix], list[Fix]]:
    """(repairable listings, listings left alone because their product is shared)."""
    fixes: list[Fix] = []
    shared: list[Fix] = []
    for listing in session.scalars(select(Listing).order_by(Listing.id)):
        if listing.product_id is None:
            continue
        size_ml, pack_count = listing_pack(listing)
        # An unparsed or implausible listing (a lone carton volume) says nothing
        # about the pack, so there is nothing to repair from it.
        if size_ml is None:
            continue
        product = session.get(Product, listing.product_id)
        if product is None:
            continue
        product_correct = (product.size_ml, product.pack_count) == (size_ml, pack_count)
        if product_correct and listing.pack_count == pack_count:
            continue
        fix = Fix(listing, size_ml, pack_count)
        if product_correct or not _other_listings(session, listing):
            fixes.append(fix)
        else:
            shared.append(fix)
    return fixes, shared


def _is_empty(session: Session, product: Product) -> bool:
    """Whether nothing refers to the product any more."""
    for model in (Listing, Observation):
        remaining = session.scalar(
            select(func.count()).select_from(model).where(model.product_id == product.id)
        )
        if remaining:
            return False
    return True


def _ledger_rows(session: Session, product_id: int) -> int:
    return session.scalar(
        select(func.count())
        .select_from(PriceDaily)
        .where(PriceDaily.product_id == product_id)
    ) or 0


def _move_references(
    session: Session, old: Product, target: Product, fix: Fix
) -> dict[str, int]:
    """Point every reference that described the bad pack at the corrected row."""
    moved = {"observations": 0, "price_daily": 0, "alerts": 0, "barcodes": 0}
    ledger_before = _ledger_rows(session, old.id)

    # Oldest first so the newest sighting ends up as the target's day row, the
    # way record_daily_price() resolves same-day writes.
    for observation in session.scalars(
        select(Observation)
        .where(Observation.product_id == old.id)
        .order_by(Observation.observed_at, Observation.id)
    ):
        # The ledger writers decide a day from the observation's status, and an
        # accepted observation is its own "still accepted" replacement - so the
        # price is withdrawn under a temporary status that is restored before
        # the session can commit, then re-recorded against the new product.
        status = observation.status
        observation.status = "rejected"
        session.flush()
        withdraw_daily_price(session, observation)
        observation.product_id = target.id
        observation.unit_price_cents_per_l = per_litre_cents(
            observation.price_cents, fix.size_ml, fix.pack_count
        )
        observation.status = status
        sync_daily_price(session, observation)
        moved["observations"] += 1
    # Rows the writers could not re-record land in the target's ledger instead,
    # so what left the old product is what the repair actually rewrote.
    moved["price_daily"] = max(0, ledger_before - _ledger_rows(session, old.id))

    moved["alerts"] = session.execute(
        update(PriceAlert).where(PriceAlert.product_id == old.id).values(product_id=target.id)
    ).rowcount
    # A family alert names no product, so it has to follow the family slug too;
    # left on the old one it would watch a family no price is recorded against.
    if old.family_slug and old.family_slug != target.family_slug:
        session.execute(
            update(PriceAlert)
            .where(PriceAlert.family_slug == old.family_slug)
            .values(family_slug=target.family_slug)
        )

    moved["barcodes"] = session.execute(
        update(Barcode).where(Barcode.product_id == old.id).values(product_id=target.id)
    ).rowcount
    # products.barcode is unique, so the code moves across in two steps with a
    # flush between them (the same order scripts/merge_duplicates.py uses).
    if target.barcode is None and old.barcode:
        code, normalised = old.barcode, old.normalised_barcode
        old.barcode = None
        old.normalised_barcode = None
        session.flush()
        target.barcode = code
        target.normalised_barcode = normalised or normalize_barcode(code)

    session.execute(
        update(Product)
        .where(Product.canonical_product_id == old.id)
        .values(canonical_product_id=target.id)
    )
    return moved


def repair(session: Session, fix: Fix) -> tuple[Product | None, dict[str, int]]:
    """Move one listing onto the product its title describes.

    Returns (new product, rows moved); an empty mapping means the fix could not
    be placed and nothing was written.
    """
    listing = fix.listing
    old = session.get(Product, listing.product_id) if listing.product_id else None
    target, _ = match_product(
        session,
        name=listing.title_raw or "",
        brand=old.brand if old is not None else None,
        size_ml=fix.size_ml,
        pack_count=fix.pack_count,
        category=old.category if old is not None else "energy",
    )
    if target is None:
        return None, {}

    moved: dict[str, int] = {}
    if old is not None and old.id != target.id:
        moved = _move_references(session, old, target, fix)

    listing.product_id = target.id
    listing.pack_count = fix.pack_count
    session.flush()

    if old is not None and old.id != target.id and _is_empty(session, old):
        old.is_active = False
        moved["retired"] = 1
    return target, moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-derive pack counts scraped as a single can")
    parser.add_argument("--apply", action="store_true", help="write the repairs (default: report)")
    args = parser.parse_args(argv)

    with session_scope() as session:
        fixes, shared = pending_fixes(session)
        for fix in shared:
            product = session.get(Product, fix.listing.product_id)
            print(
                f"listing {fix.listing.id} {fix.listing.title_raw!r}: left alone, "
                f"{product.slug} is shared with other listings"
            )
        if not fixes:
            print("no listings whose stored pack disagrees with their title")
            return 0

        for fix in fixes:
            listing = fix.listing
            old = session.get(Product, listing.product_id) if listing.product_id else None
            was = (
                f"{old.slug} ({old.size_ml or '?'}mL x{old.pack_count})"
                if old is not None
                else "no product"
            )
            target, moved = repair(session, fix)
            if target is None:
                print(f"listing {listing.id} {listing.title_raw!r}: no product could be derived")
                continue
            detail = ", ".join(f"{kind} {count}" for kind, count in moved.items() if count)
            print(
                f"listing {listing.id} {listing.title_raw!r}: {was} -> "
                f"{target.slug} ({fix.size_ml}mL x{fix.pack_count})"
                + (f"; {detail}" if detail else "")
            )

        print(
            ("repaired " if args.apply else "would repair ")
            + f"{len(fixes)} listing(s); observations are repriced and ledger rows follow them"
        )
        if not args.apply:
            # Report only: undo the work, as scripts/clean_names.py and
            # scripts/merge_duplicates.py do.
            session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
