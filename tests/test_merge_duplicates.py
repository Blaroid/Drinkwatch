"""Folding the duplicate plain-SKU rows the brand-only bug left behind.

The repair is plain SQLAlchemy over product references, so an in-memory SQLite
database with the tables it touches is enough. The script is never run as a
whole against Postgres here.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Iterator

import pytest
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Barcode,
    Listing,
    Observation,
    PriceAlert,
    PriceDaily,
    Product,
)
from scripts import merge_duplicates

TABLES = (
    Product.__table__,
    Listing.__table__,
    Observation.__table__,
    Barcode.__table__,
    PriceDaily.__table__,
    PriceAlert.__table__,
)

DAY = date(2026, 11, 18)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=TABLES)
    # Migration 012 replaces price_daily's primary key with a unique index so a
    # store can hold its own row for a day. SQLite cannot alter a primary key, so
    # rebuild the table the way the deployed database has it.
    with engine.begin() as conn:
        conn.exec_driver_sql("create table price_daily_nopk as select * from price_daily")
        conn.exec_driver_sql("drop table price_daily")
        conn.exec_driver_sql("alter table price_daily_nopk rename to price_daily")
        conn.exec_driver_sql(
            "create unique index uq_price_daily_day_chain_product_store"
            " on price_daily (day, chain_id, product_id, store_id)"
        )
    with Session(engine, expire_on_commit=False) as db:
        yield db


def _product(
    db: Session, slug: str, *, name: str = "Original", size_ml: int = 473, is_active: bool = True
) -> Product:
    product = Product(
        slug=slug,
        brand="Red Bull",
        name=name,
        size_ml=size_ml,
        pack_count=1,
        is_active=is_active,
    )
    db.add(product)
    db.flush()
    return product


def _duplicate_pair(db: Session) -> tuple[Product, Product]:
    """The audit's red-bull-473ml / red-bull-original-473ml pair."""
    survivor = _product(db, "red-bull-473ml")
    loser = _product(db, "red-bull-original-473ml")
    return survivor, loser


def _add_daily(
    db: Session,
    product_id: int,
    *,
    chain_id: int = 2,
    store_id: int | None = None,
    price_cents: int = 450,
) -> None:
    # Row inserts, not objects: the ORM key has no store in it, so two stores'
    # rows for the same day would collide in the identity map.
    db.execute(
        insert(PriceDaily).values(
            day=DAY,
            chain_id=chain_id,
            product_id=product_id,
            store_id=store_id,
            price_cents=price_cents,
        )
    )


def _daily(db: Session, product_id: int) -> list[tuple[int, int | None, int]]:
    rows = db.execute(
        select(PriceDaily.chain_id, PriceDaily.store_id, PriceDaily.price_cents)
        .where(PriceDaily.product_id == product_id)
        .order_by(PriceDaily.chain_id, PriceDaily.store_id)
    ).all()
    return [(chain_id, store_id, price_cents) for chain_id, store_id, price_cents in rows]


def _observation(db: Session, obs_id: int, product: Product) -> Observation:
    # observations.id is a BigInteger primary key, which SQLite does not fill in;
    # the price varies so each sighting gets its own dedupe key, as in the app.
    price_cents = 550 + obs_id
    row = Observation(
        id=obs_id,
        chain_id=1,
        product_id=product.id,
        price_cents=price_cents,
        promo_kind="none",
        price_scope="chain",
        source="scrape",
        dedupe_key=(
            f"scrape:chain1:unscoped:{product.id}:2026-11-18:{price_cents}:none:0:0"
        ),
    )
    db.add(row)
    return row


def test_plain_duplicate_pairs_only_picks_live_unlinked_plain_rows(session: Session) -> None:
    survivor, loser = _duplicate_pair(session)
    # Genuinely different flavours share a brand/size/pack and must stay apart.
    _product(session, "monster-ultra-500ml", name="Ultra", size_ml=500)
    _product(session, "monster-ultra-violet-500ml", name="Ultra Violet", size_ml=500)
    # An already-merged row is curation, not a new duplicate.
    _product(session, "red-bull-330ml", size_ml=330, is_active=False)
    linked = _product(session, "red-bull-473ml-alias", size_ml=473)
    linked.canonical_product_id = survivor.id

    pairs = merge_duplicates.plain_duplicate_pairs(session)

    assert [(pair.survivor.id, pair.loser.id) for pair in pairs] == [
        (survivor.id, loser.id)
    ]


def test_merge_moves_every_reference_and_deactivates_the_double(session: Session) -> None:
    survivor, loser = _duplicate_pair(session)
    # The loser scraped this code first, so products.barcode holds it.
    loser.barcode = "9300675001234"
    listing = Listing(chain_id=2, external_sku="coles-1", product_id=loser.id)
    barcode = Barcode(barcode="9300675001234", product_id=loser.id, source="scrape")
    alert = PriceAlert(device_id=1, product_id=loser.id)
    session.add_all([listing, barcode, alert])
    _add_daily(session, loser.id, price_cents=560)
    _observation(session, 1, loser)
    _observation(session, 2, loser)
    session.flush()

    moved = merge_duplicates.merge_pair(
        session, merge_duplicates.Pair(survivor, loser)
    )

    assert moved == {
        "listings": 1,
        "observations": 2,
        "barcodes": 1,
        "alerts": 1,
        "aliases": 0,
        "price_daily": 1,
    }
    # Flush before re-reading: expiring first would drop the pending updates.
    session.flush()
    session.expire_all()
    assert session.get(Listing, listing.id).product_id == survivor.id
    assert session.get(Barcode, "9300675001234").product_id == survivor.id
    assert session.get(PriceAlert, alert.id).product_id == survivor.id
    assert _daily(session, survivor.id) == [(2, None, 560)]
    assert session.scalars(
        select(Observation.id).where(Observation.product_id == survivor.id)
    ).all() == [1, 2]
    assert session.get(Product, loser.id).is_active is False
    assert session.get(Product, survivor.id).is_active is True
    assert session.get(Product, survivor.id).barcode == "9300675001234"
    assert session.get(Product, loser.id).barcode is None


def test_merge_keeps_the_survivors_daily_row_when_a_day_collides(session: Session) -> None:
    survivor, loser = _duplicate_pair(session)
    _add_daily(session, survivor.id, chain_id=3, price_cents=450)
    _add_daily(session, loser.id, chain_id=3, price_cents=560)
    _add_daily(session, loser.id, chain_id=4, price_cents=560)

    moved = merge_duplicates.merge_pair(
        session, merge_duplicates.Pair(survivor, loser)
    )

    assert moved["price_daily"] == 1
    assert _daily(session, survivor.id) == [(3, None, 450), (4, None, 560)]


def test_merge_moves_store_scoped_days_one_store_at_a_time(session: Session) -> None:
    # price_daily rows for the same day and chain differ only by store, so the
    # merge must not drop or move a store row because a sibling store collided.
    survivor, loser = _duplicate_pair(session)
    _add_daily(session, survivor.id, store_id=7, price_cents=450)
    _add_daily(session, loser.id, store_id=7, price_cents=560)
    _add_daily(session, loser.id, store_id=8, price_cents=570)

    moved = merge_duplicates.merge_pair(
        session, merge_duplicates.Pair(survivor, loser)
    )

    assert moved["price_daily"] == 1
    assert _daily(session, survivor.id) == [(2, 7, 450), (2, 8, 570)]
    assert _daily(session, loser.id) == []


def test_dry_run_reports_the_pairs_and_writes_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    survivor, loser = _duplicate_pair(session)
    session.add(Listing(chain_id=2, external_sku="coles-1", product_id=loser.id))
    session.commit()

    @contextmanager
    def _scope() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(merge_duplicates, "session_scope", _scope)

    assert merge_duplicates.main([]) == 0

    out = capsys.readouterr().out
    assert "red-bull-473ml <- red-bull-original-473ml: listings 1" in out
    assert "would merge 1 duplicate pair(s)" in out
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Product)) == 2
    assert session.get(Product, loser.id).is_active is True
    assert session.scalars(select(Listing.product_id)).all() == [loser.id]
