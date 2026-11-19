"""The daily price ledger: store rows plus the chain row that summarises them.

``price_daily`` exists so a price that does not change for weeks still has
history for the 90-day deal median. ``observations`` is append-only and deduped,
so it cannot answer "what did this cost on this day" on its own.

Two scopes share the table:

* store rows (``store_id`` set): what one shop charged that day.
* chain rows (``store_id`` NULL): what the chain charged, either because it
  publishes a single price (Woolworths, Coles) or because the row was
  aggregated from the chain's stores.

Both writers - the scrapers and the crowd API - go through here so the chain
row can never drift from the store rows it summarises.
"""

from __future__ import annotations

import zoneinfo
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .models import PriceDaily

PerthTZ = zoneinfo.ZoneInfo("Australia/Perth")

# The chain row is the median store, not the cheapest one: the cheapest store
# would make every offer in the chain look typical, and the median is the same
# statistic the deal badge asks the history for.
#
# `where price_daily.source = 'aggregate'` is the precedence rule: a chain that
# published one price state-wide (Woolworths, Coles; source is the scraper) owns
# its chain row for the day, and a later store sighting must not replace it.
# Aggregate rows are ours to keep current.
_AGGREGATE_SQL = text(
    """
    insert into price_daily
        (day, chain_id, product_id, store_id, price_cents, was_price_cents, promo_kind, source)
    select day,
           chain_id,
           product_id,
           null,
           round(percentile_cont(0.5) within group (order by price_cents))::int,
           null,
           'none',
           'aggregate'
      from price_daily
     where day = :day
       and chain_id = :chain_id
       and product_id = :product_id
       and store_id is not null
     group by day, chain_id, product_id
    on conflict (day, chain_id, product_id, store_id) do update
       set price_cents = excluded.price_cents,
           was_price_cents = excluded.was_price_cents,
           promo_kind = excluded.promo_kind,
           source = excluded.source
       where price_daily.source = 'aggregate'
    """
)


def record_daily_price(
    session: Session,
    *,
    product_id: int,
    chain_id: int,
    price_cents: int,
    store_id: int | None = None,
    was_price_cents: int | None = None,
    promo_kind: str = "none",
    source: str = "scrape",
    observed_at: datetime | None = None,
) -> None:
    """Write the day's row for this scope, then refresh the chain row behind it.

    Within one scope the last writer of the day wins, which is honest: a store
    that reprices twice in a day ends on the later price. Before migration 008
    the same rule applied across stores, which is what collapsed seven SpudShed
    shops (and every crowd sighting) into one arbitrary price.
    """
    if price_cents <= 0:
        return
    observed_at = observed_at or datetime.now(PerthTZ)
    day = observed_at.astimezone(PerthTZ).date()

    insert = pg_insert(PriceDaily).values(
        day=day,
        chain_id=chain_id,
        product_id=product_id,
        store_id=store_id,
        price_cents=price_cents,
        was_price_cents=was_price_cents,
        promo_kind=promo_kind,
        source=source,
    )
    session.execute(
        insert.on_conflict_do_update(
            index_elements=["day", "chain_id", "product_id", "store_id"],
            set_={
                "price_cents": insert.excluded.price_cents,
                "was_price_cents": insert.excluded.was_price_cents,
                "promo_kind": insert.excluded.promo_kind,
                "source": insert.excluded.source,
            },
        )
    )

    if store_id is not None:
        # Recomputed from the store rows known so far, so a later store in the
        # same run (SpudShed scrapes seven) updates the chain row instead of
        # overwriting a different shop's price.
        session.execute(
            _AGGREGATE_SQL,
            {"day": day, "chain_id": chain_id, "product_id": product_id},
        )
