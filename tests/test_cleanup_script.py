"""cleanup.py purges the daily ledger along with the observations it deletes.

The purge is plain SQLAlchemy over the tables it touches, so an in-memory SQLite
database is enough. The chain-row rebuild is stubbed in the apply test: that rule
lives in app.daily (and is tested there) and its aggregate SQL is Postgres-only.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Iterator

import pytest
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.orm import Session

from app.models import Base, Chain, Listing, Observation, PriceDaily, Product
from scripts import cleanup

TABLES = (
    Chain.__table__,
    Product.__table__,
    Listing.__table__,
    Observation.__table__,
    PriceDaily.__table__,
)

DAY = date(2026, 11, 19)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=TABLES)
    # A chain row (store_id NULL) and its store rows share day+chain+product, so
    # rebuild price_daily the way migration 012 keys the deployed table.
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


def _chain(session: Session, code: str) -> Chain:
    chain = Chain(code=code, name=code.title())
    session.add(chain)
    session.flush()
    return chain


def _product(session: Session, slug: str) -> Product:
    # source=manual: cleanup deletes orphaned *auto* products, and this test is
    # about the ledger, not the product sweep.
    product = Product(slug=slug, brand="Monster", name="Ultra", size_ml=500, source="manual")
    session.add(product)
    session.flush()
    return product


def _observation(
    session: Session,
    obs_id: int,
    product: Product,
    chain: Chain,
    *,
    source: str,
    store_id: int | None,
    price_cents: int = 450,
) -> Observation:
    observation = Observation(
        id=obs_id,
        chain_id=chain.id,
        product_id=product.id,
        store_id=store_id,
        price_cents=price_cents,
        price_scope="store" if store_id is not None else "chain",
        source=source,
        status="accepted",
        dedupe_key=f"{source}:{obs_id}",
    )
    session.add(observation)
    session.flush()
    return observation


def _daily(
    session: Session,
    product: Product,
    chain: Chain,
    *,
    store_id: int | None,
    source: str,
    price_cents: int = 450,
) -> None:
    # Row inserts: the ORM identity has no store in it, so two stores' rows for
    # the same day would collide in the identity map.
    session.execute(
        insert(PriceDaily).values(
            day=DAY,
            chain_id=chain.id,
            product_id=product.id,
            store_id=store_id,
            price_cents=price_cents,
            source=source,
        )
    )


@contextmanager
def _scope(session: Session) -> Iterator[Session]:
    yield session


def _seed(session: Session) -> tuple[Chain, Chain, Chain, Product]:
    woolworths = _chain(session, "woolworths")
    coles = _chain(session, "coles")
    spudshed = _chain(session, "spudshed")
    product = _product(session, "monster-ultra-500ml")
    _observation(session, 1, product, woolworths, source="scrape", store_id=None)
    _observation(session, 2, product, woolworths, source="scrape", store_id=None)
    # A crowd sighting at the same chain keeps its store row, so the chain row
    # still has something to summarise.
    _observation(session, 3, product, woolworths, source="crowd", store_id=77)
    _observation(session, 4, product, spudshed, source="scrape", store_id=None)

    _daily(session, product, woolworths, store_id=None, source="scrape")
    _daily(session, product, woolworths, store_id=77, source="crowd", price_cents=470)
    _daily(session, product, spudshed, store_id=None, source="scrape", price_cents=500)
    session.flush()
    return woolworths, coles, spudshed, product


def test_dry_run_counts_the_ledger_rows_and_deletes_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(session)
    monkeypatch.setattr(cleanup, "session_scope", lambda: _scope(session))

    assert cleanup.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "2 scraped observations" in out
    assert "1 price_daily rows" in out
    assert "will reconcile 1 chain row(s)" in out
    assert session.scalar(select(func.count()).select_from(PriceDaily)) == 3
    assert session.scalar(select(func.count()).select_from(Observation)) == 4


def test_purge_removes_scraped_ledger_rows_and_rebuilds_the_chain_row(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    woolworths, _coles, spudshed, product = _seed(session)
    rebuilt: list[tuple[date, int, int]] = []
    monkeypatch.setattr(
        cleanup,
        "_refresh_chain_row",
        lambda _session, day, chain_id, product_id: rebuilt.append((day, chain_id, product_id)),
    )
    monkeypatch.setattr(cleanup, "session_scope", lambda: _scope(session))

    assert cleanup.main([]) == 0

    remaining = session.execute(
        select(PriceDaily.chain_id, PriceDaily.store_id, PriceDaily.source).order_by(
            PriceDaily.chain_id, PriceDaily.store_id
        )
    ).all()
    assert [(chain_id, store_id, source) for chain_id, store_id, source in remaining] == [
        (woolworths.id, 77, "crowd"),
        (spudshed.id, None, "scrape"),
    ]
    # The store row survived, so the chain row has to be summarised again.
    assert rebuilt == [(DAY, woolworths.id, product.id)]
    # Scraped rows leave with their observations; the crowd sighting stays.
    assert session.scalars(select(Observation.id).order_by(Observation.id)).all() == [3, 4]
    assert session.scalar(select(func.count()).select_from(Listing)) == 0
