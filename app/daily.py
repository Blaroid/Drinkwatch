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
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .models import Observation, PriceDaily

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
    day = local_day(observed_at)

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


def local_day(observed_at: datetime) -> date:
    """The ledger day a sighting belongs to, in the timezone the ledger is keyed by."""
    return observed_at.astimezone(PerthTZ).date()


def sync_daily_price(session: Session, observation: Observation) -> None:
    """Make the day's ledger row agree with the observation's current status.

    The ledger used to be written once, at insert, so a moderation decision
    never reached it: approving a flagged price added no history (no deal badge
    and no alert), and rejecting an accepted one left the wrong price in the
    90-day median and the alert baseline. Callers set ``observation.status``
    first; this reads it back. Every status transition goes through here so
    both the admin endpoint and the vote tally keep the ledger in step.
    """
    if observation.status == "accepted":
        record_daily_price(
            session,
            product_id=observation.product_id,
            chain_id=observation.chain_id,
            store_id=observation.store_id,
            price_cents=observation.price_cents,
            was_price_cents=observation.was_price_cents,
            promo_kind=observation.promo_kind,
            source=observation.source,
            observed_at=observation.observed_at,
        )
    else:
        withdraw_daily_price(session, observation)


def withdraw_daily_price(session: Session, observation: Observation) -> None:
    """Take a no-longer-accepted observation back out of the day's ledger.

    A day holds one row per scope, written last-writer-wins, so this is not a
    blind delete: it only acts when the row currently reflects this observation
    (same source and price), and then restores the newest observation still
    accepted for that day, or removes the row when none is left. A row written
    by some later scrape belongs to that scrape and is left alone.
    """
    # Sessions are autoflush=False, so the status the caller just set is still
    # pending; the queries below must not see the row as accepted again.
    session.flush()
    day = local_day(observation.observed_at)
    current = session.scalar(
        select(PriceDaily).where(
            PriceDaily.day == day,
            PriceDaily.chain_id == observation.chain_id,
            PriceDaily.product_id == observation.product_id,
            PriceDaily.store_id.is_(None)
            if observation.store_id is None
            else PriceDaily.store_id == observation.store_id,
        )
    )
    if current is None:
        return
    if current.source != observation.source or current.price_cents != observation.price_cents:
        return

    day_start = datetime.combine(day, time.min, tzinfo=PerthTZ)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=PerthTZ)
    replacement = session.scalar(
        select(Observation)
        .where(
            Observation.chain_id == observation.chain_id,
            Observation.product_id == observation.product_id,
            Observation.store_id.is_(None)
            if observation.store_id is None
            else Observation.store_id == observation.store_id,
            Observation.status == "accepted",
            Observation.observed_at >= day_start,
            Observation.observed_at < day_end,
        )
        .order_by(Observation.observed_at.desc(), Observation.id.desc())
        .limit(1)
    )
    if replacement is not None:
        record_daily_price(
            session,
            product_id=replacement.product_id,
            chain_id=replacement.chain_id,
            store_id=replacement.store_id,
            price_cents=replacement.price_cents,
            was_price_cents=replacement.was_price_cents,
            promo_kind=replacement.promo_kind,
            source=replacement.source,
            observed_at=replacement.observed_at,
        )
        return

    session.execute(
        delete(PriceDaily).where(
            PriceDaily.day == day,
            PriceDaily.chain_id == observation.chain_id,
            PriceDaily.product_id == observation.product_id,
            PriceDaily.store_id.is_(None)
            if observation.store_id is None
            else PriceDaily.store_id == observation.store_id,
        )
    )
    # Store scope leaves a chain aggregate behind; chain scope may have removed
    # the chain row a per-store day was summarised into. Either way the chain
    # row has to be rebuilt (or dropped) from the store rows that remain.
    _refresh_chain_row(session, day, observation.chain_id, observation.product_id)


def _refresh_chain_row(session: Session, day: date, chain_id: int, product_id: int) -> None:
    """Recompute the chain row after one of its store rows left the day.

    _AGGREGATE_SQL only writes when store rows remain, so the last store row
    leaving a day would otherwise leave its aggregate behind, still feeding the
    deal median with a price no accepted observation supports.
    """
    remaining = session.scalar(
        select(func.count())
        .select_from(PriceDaily)
        .where(
            PriceDaily.day == day,
            PriceDaily.chain_id == chain_id,
            PriceDaily.product_id == product_id,
            PriceDaily.store_id.is_not(None),
        )
    )
    if remaining:
        session.execute(
            _AGGREGATE_SQL,
            {"day": day, "chain_id": chain_id, "product_id": product_id},
        )
        return
    session.execute(
        delete(PriceDaily).where(
            PriceDaily.day == day,
            PriceDaily.chain_id == chain_id,
            PriceDaily.product_id == product_id,
            PriceDaily.store_id.is_(None),
            PriceDaily.source == "aggregate",
        )
    )
