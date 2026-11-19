"""Remove scraper noise: non-energy products and their price rows.

Run after changing the product-matching rules, then re-run the scrapers.

The daily ledger is the part a re-scrape cannot repair on its own: ``price_daily``
is what the 90-day deal badge and the alert median read, so a row left behind by
a purged scrape keeps feeding both with a price no observation supports. The
purge covers it, and rebuilds the chain rows that summarised the store rows it
removes (a crowd sighting at the same shop still belongs in them).

    python scripts/cleanup.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, ".")

# The chain row has one rule and it lives in app.daily; a purge that restated
# "median of the remaining stores, unless the chain published a price" would be
# a second place to get it wrong.
from app.daily import _refresh_chain_row  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import Chain, Listing, Observation, PriceDaily, Product  # noqa: E402

TARGET_CHAINS = ("woolworths", "coles")


def scraped_ledger_groups(session: Session, chain_ids: list[int]) -> list[tuple[date, int, int]]:
    """The day+chain+product rows whose chain row this purge invalidates.

    Scraped rows are what gets deleted, but the chain aggregate may have been
    computed from them, so every group they touch has to be reconciled from the
    store rows that remain - a chain-price row leaves nothing behind, while a
    crowd sighting at one of the chain's shops stays and keeps its aggregate.
    """
    return [
        (day, chain_id, product_id)
        for day, chain_id, product_id in session.execute(
            select(PriceDaily.day, PriceDaily.chain_id, PriceDaily.product_id)
            .where(
                PriceDaily.chain_id.in_(chain_ids),
                PriceDaily.source == "scrape",
            )
            .distinct()
        ).all()
    ]


def _count(session: Session, table, *criteria) -> int:
    return int(session.scalar(select(func.count()).select_from(table).where(*criteria)) or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge scraper noise")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    with session_scope() as session:
        chain_ids = list(
            session.scalars(select(Chain.id).where(Chain.code.in_(TARGET_CHAINS)))
        )
        scraped = (Observation.source == "scrape", Observation.chain_id.in_(chain_ids))
        ledger = (PriceDaily.chain_id.in_(chain_ids), PriceDaily.source == "scrape")
        obs = _count(session, Observation, *scraped)
        listings = _count(session, Listing, Listing.chain_id.in_(chain_ids))
        prices = _count(session, PriceDaily, *ledger)
        groups = scraped_ledger_groups(session, chain_ids)
        print(
            f"will delete {obs} scraped observations, {listings} listings and "
            f"{prices} price_daily rows for {TARGET_CHAINS}"
        )
        print(f"will reconcile {len(groups)} chain row(s) from the store rows that remain")
        if args.dry_run:
            return 0

        session.execute(delete(Observation).where(*scraped))
        session.execute(delete(Listing).where(Listing.chain_id.in_(chain_ids)))
        session.execute(delete(PriceDaily).where(*ledger))
        for day, chain_id, product_id in groups:
            _refresh_chain_row(session, day, chain_id, product_id)

        orphans = list(
            session.scalars(
                select(Product).where(
                    Product.source == "auto",
                    ~Product.id.in_(select(Listing.product_id).where(Listing.product_id.is_not(None))),
                    ~Product.id.in_(select(Observation.product_id)),
                )
            )
        )
        for product in orphans:
            session.delete(product)
        print(f"deleted {len(orphans)} orphaned auto products")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
