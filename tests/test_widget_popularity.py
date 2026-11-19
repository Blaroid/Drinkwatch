"""Widget default list ranks drinks by distinct stores, not by sighting count.

The route has no other ranking signal, so seed an in-memory SQLite database and
stub the price helpers: the order of ``items`` is what is under test, not the
price lookup (which is Postgres-only SQL).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import public
from app.models import Base, Chain, Observation, Product, Store
from app.schemas import OfferOut

NOW = datetime.now(timezone.utc)
WIDGET_URL = "/v1/widget?lat=-31.9523&lon=115.8613"


@pytest.fixture()
def session() -> Iterator[Session]:
    # StaticPool keeps one connection alive so the in-memory schema survives
    # across the TestClient's worker thread.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Only the tables this query touches: the rest of the metadata uses JSONB,
    # which SQLite cannot compile.
    Base.metadata.create_all(
        engine,
        tables=[Chain.__table__, Store.__table__, Product.__table__, Observation.__table__],
    )
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def _chains_and_stores(db: Session) -> None:
    db.add_all(
        [
            Chain(id=1, code="spudshed", name="SpudShed", kind="independent"),
            Chain(id=2, code="woolworths", name="Woolworths"),
            Chain(id=3, code="7eleven", name="7-Eleven", kind="convenience"),
            Chain(id=4, code="coles", name="Coles"),
        ]
    )
    db.add_all(
        [
            Store(id=1, chain_id=1, name="SpudShed Wangara", suburb="Wangara"),
            Store(id=2, chain_id=1, name="SpudShed Joondalup", suburb="Joondalup"),
            Store(id=3, chain_id=3, name="7-Eleven Morley", suburb="Morley"),
        ]
    )


def _product(db: Session, product_id: int, slug: str, brand: str, name: str) -> Product:
    product = Product(
        id=product_id,
        slug=slug,
        brand=brand,
        name=name,
        size_ml=500,
        category="energy",
        is_active=True,
    )
    db.add(product)
    return product


def _obs(
    db: Session,
    obs_id: int,
    *,
    product_id: int,
    store_id: int | None,
    chain_id: int,
    hours_ago: int = 0,
) -> None:
    db.add(
        Observation(
            id=obs_id,
            store_id=store_id,
            chain_id=chain_id,
            product_id=product_id,
            price_cents=350,
            source="scrape",
            price_scope="store" if store_id is not None else "chain",
            observed_at=NOW - timedelta(hours=hours_ago),
        )
    )


def _offer(product: Product) -> OfferOut:
    return OfferOut(
        store_id=1,
        store_name="SpudShed Wangara",
        chain_code="spudshed",
        chain_name="SpudShed",
        chain_kind="independent",
        suburb="Wangara",
        distance_km=2.5,
        price_cents=350,
        price="$3.50",
        promo_kind="none",
        source="scrape",
        price_scope="store",
        age_hours=1.0,
        observed_at=NOW - timedelta(hours=1),
    )


def _client(db: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(public.router)
    app.dependency_overrides[public.get_session] = lambda: db
    monkeypatch.setattr(public, "nearby_stores", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        public,
        "_offers_for_product",
        lambda _session, product, _stores, _since: [_offer(product)],
    )
    return TestClient(app)


def _slugs(db: Session, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    body = _client(db, monkeypatch).get(WIDGET_URL).json()
    return [item["slug"] for item in body["items"]]


def test_repeated_scrapes_do_not_outrank_more_stores(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four SpudShed scrapes at one shop must lose to two shops seen once each."""
    _chains_and_stores(session)
    monster = _product(session, 1, "monster-ultra", "Monster", "Ultra")
    red_bull = _product(session, 2, "red-bull", "Red Bull", "Original")
    v = _product(session, 3, "v-sugarfree", "V", "Sugarfree")
    for hour in range(4):
        _obs(session, 100 + hour, product_id=monster.id, store_id=1, chain_id=1, hours_ago=hour)
    _obs(session, 110, product_id=red_bull.id, store_id=1, chain_id=1)
    _obs(session, 111, product_id=red_bull.id, store_id=3, chain_id=3)
    # A servo-only drink: one shop, so it ranks but never dominates.
    _obs(session, 112, product_id=v.id, store_id=3, chain_id=3)
    session.commit()

    assert _slugs(session, monkeypatch) == ["red-bull", "monster-ultra", "v-sugarfree"]


def test_chain_wide_price_counts_once_per_chain(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Woolworths/Coles rows have no store_id; three scrapes are not three shops."""
    _chains_and_stores(session)
    wide = _product(session, 1, "wide", "Alpha", "Two Stores")
    local = _product(session, 2, "local", "Beta", "One Store")
    woolies = _product(session, 3, "woolies", "Zeta", "Online Only")
    _obs(session, 100, product_id=wide.id, store_id=1, chain_id=1)
    _obs(session, 101, product_id=wide.id, store_id=2, chain_id=1)
    _obs(session, 102, product_id=local.id, store_id=1, chain_id=1)
    for day in range(3):
        _obs(
            session,
            110 + day,
            product_id=woolies.id,
            store_id=None,
            chain_id=2,
            hours_ago=day * 24,
        )
    session.commit()

    # woolies scores 1, not 3: if chain rows were counted per row it would lead.
    assert _slugs(session, monkeypatch) == ["wide", "local", "woolies"]


def test_chain_wide_rows_from_two_chains_add_up(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _chains_and_stores(session)
    both = _product(session, 1, "both-chains", "Alpha", "Online Both")
    one_store = _product(session, 2, "one-store", "Beta", "One Store")
    _obs(session, 100, product_id=both.id, store_id=None, chain_id=2)
    _obs(session, 101, product_id=both.id, store_id=None, chain_id=4)
    _obs(session, 102, product_id=one_store.id, store_id=1, chain_id=1)
    session.commit()

    assert _slugs(session, monkeypatch) == ["both-chains", "one-store"]


def test_pending_products_never_reach_the_default_widget(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crowd-submitted can is not pickable until an admin reviews it."""
    _chains_and_stores(session)
    _product(session, 1, "monster-ultra", "Monster", "Ultra")
    pending = _product(session, 2, "monster-smoke-test-500ml", "Monster", "Smoke Test")
    pending.status = "pending"
    _obs(session, 100, product_id=1, store_id=1, chain_id=1)
    _obs(session, 101, product_id=2, store_id=1, chain_id=1)
    session.commit()

    # The price stub answers for any catalogue row, so a pending drink reaching
    # the default list would show up here.
    assert _slugs(session, monkeypatch) == ["monster-ultra"]
