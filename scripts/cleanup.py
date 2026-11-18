"""Remove scraper noise: non-energy products and their price rows.

Run after changing the product-matching rules, then re-run the scrapers.

    python scripts/cleanup.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import delete, select, text

sys.path.insert(0, ".")

from app.db import session_scope  # noqa: E402
from app.models import Chain, Listing, Observation, Product  # noqa: E402

TARGET_CHAINS = ("woolworths", "coles")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge scraper noise")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    with session_scope() as session:
        chain_ids = list(
            session.scalars(select(Chain.id).where(Chain.code.in_(TARGET_CHAINS)))
        )
        obs = session.execute(
            text(
                "select count(*) from observations where source = 'scrape' and chain_id = any(:ids)"
            ),
            {"ids": chain_ids},
        ).scalar()
        listings = session.execute(
            text("select count(*) from listings where chain_id = any(:ids)"), {"ids": chain_ids}
        ).scalar()
        print(f"will delete {obs} scraped observations and {listings} listings for {TARGET_CHAINS}")
        if args.dry_run:
            return 0

        session.execute(
            text("delete from observations where source = 'scrape' and chain_id = any(:ids)"),
            {"ids": chain_ids},
        )
        session.execute(delete(Listing).where(Listing.chain_id.in_(chain_ids)))

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
