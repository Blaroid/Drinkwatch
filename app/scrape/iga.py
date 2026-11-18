"""IGA scraper: deliberately not implemented yet.

Findings (2026-11): IGA stores are independently owned and priced. Only stores
that use the online platform at https://www.igashop.com.au expose prices, and
each store has to be selected before prices appear. There is no chain-wide feed.

Plan when we add it:
  1. Enumerate IGA stores in the DB (seeded from OpenStreetMap).
  2. For each store we care about, resolve its igashop storefront id.
  3. Poll /storefront/<id>/category/energy-drinks style pages, same parser shape
     as the SpudShed scraper.

Until then IGA prices come from crowd sightings only.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .base import ScrapeStats

log = logging.getLogger(__name__)

CHAIN_CODE = "iga"


def scrape(
    session: Session,
    store_ids: list[int] | None = None,
    keywords: list[str] | None = None,
    stats: ScrapeStats | None = None,
) -> ScrapeStats:
    stats = stats or ScrapeStats(chain=CHAIN_CODE)
    stats.errors.append("iga scraping not implemented; crowd data only for now")
    return stats
