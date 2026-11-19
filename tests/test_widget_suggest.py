"""The widget suggestion list must not cost a query per candidate.

Before this change every one of up to 60 suggest candidates went through
``_offers_for_product`` - two alias lookups, one or two price queries and a
90-day percentile query each - so a refresh with suggestions was ~180 round
trips. These tests pin the behaviour that must not move (brand-then-price
ranking, which store wins, alias groups, freshness) and prove the query count
no longer follows the size of the candidate set.

The price helpers are Postgres-only SQL, so the database is in-memory SQLite
holding just the four tables these queries touch. Only the suggestion path is
exercised for real; the catalogue rows above it are stubbed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import public
from app.api.deps import _freshest
from app.models import Base, Chain, Observation, Product, Store
from app.schemas import OfferOut

NOW = datetime.now(timezone.utc)
WIDGET_URL = "/v1/widget?lat=-31.9523&lon=115.8613&products=monster-ultra"
DISTANCES = {1: 1.0, 2: 2.5, 3: 4.0}


@pytest.fixture()
def db() -> Iterator[tuple[Engine, Session]]:
    # StaticPool keeps one connection alive so the in-memory schema survives
    # across the TestClient's worker thread.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Only the tables these queries touch: the rest of the metadata uses JSONB,
    # which SQLite cannot compile.
    Base.metadata.create_all(
        engine,
        tables=[Chain.__table__, Store.__table__, Product.__table__, Observation.__table__],
    )
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield engine, session


def _seed(session: Session) -> None:
    session.add_all(
        [
            Chain(id=1, code="spudshed", name="SpudShed", kind="independent"),
            Chain(id=2, code="woolworths", name="Woolworths"),
            Chain(id=3, code="7eleven", name="7-Eleven", kind="convenience"),
        ]
    )
    session.add_all(
        [
            Store(id=1, chain_id=1, name="SpudShed Wangara", suburb="Wangara"),
            Store(id=2, chain_id=2, name="Woolworths Perth", suburb="Perth"),
            Store(id=3, chain_id=3, name="7-Eleven Morley", suburb="Morley"),
        ]
    )


def _product(
    session: Session,
    product_id: int,
    slug: str,
    brand: str,
    name: str = "Original",
    *,
    pack_count: int = 1,
    canonical: int | None = None,
) -> Product:
    product = Product(
        id=product_id,
        slug=slug,
        brand=brand,
        name=name,
        size_ml=500,
        pack_count=pack_count,
        category="energy",
        is_active=True,
        canonical_product_id=canonical,
    )
    session.add(product)
    return product


def _obs(
    session: Session,
    obs_id: int,
    *,
    product_id: int,
    price_cents: int,
    store_id: int | None = None,
    chain_id: int = 1,
    hours_ago: int = 1,
    status: str = "accepted",
) -> None:
    session.add(
        Observation(
            id=obs_id,
            store_id=store_id,
            chain_id=chain_id,
            product_id=product_id,
            price_cents=price_cents,
            source="scrape",
            price_scope="store" if store_id is not None else "chain",
            status=status,
            observed_at=NOW - timedelta(hours=hours_ago),
        )
    )


def _store_distances(session: Session) -> list[tuple[Store, float]]:
    # Load through the ORM so Store.chain is joined eagerly: a lazily loaded
    # chain would add a query per store inside the request under test.
    return [
        (store, DISTANCES[store.id])
        for store in session.scalars(select(Store).order_by(Store.id))
    ]


def _catalogue_offer(product: Product) -> OfferOut:
    return OfferOut(
        store_id=1,
        store_name="SpudShed Wangara",
        chain_code="spudshed",
        chain_name="SpudShed",
        chain_kind="independent",
        suburb="Wangara",
        distance_km=1.0,
        price_cents=355,
        price="$3.55",
        promo_kind="none",
        source="scrape",
        price_scope="store",
        age_hours=1.0,
        observed_at=NOW - timedelta(hours=1),
    )


def _client(
    session: Session, store_distances: list[tuple[Store, float]], monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    app = FastAPI()
    app.include_router(public.router)
    app.dependency_overrides[public.get_session] = lambda: session
    monkeypatch.setattr(public, "nearby_stores", lambda *args, **kwargs: store_distances)

    def _catalogue_only(_session, product, _stores, _since):
        # The catalogue row above the suggestion list is stubbed because the
        # single-product price helper is Postgres-only SQL. Everything else must
        # be batched: reaching this stub for a suggest candidate means a query
        # per candidate is back.
        if product.slug != "monster-ultra":
            raise AssertionError(
                "suggestion candidates must be priced in bulk, not one _offers_for_product call each"
            )
        return [_catalogue_offer(product)]

    monkeypatch.setattr(public, "_offers_for_product", _catalogue_only)
    return TestClient(app)


def _widget(session: Session, monkeypatch: pytest.MonkeyPatch, suggest: int = 8) -> dict:
    client = _client(session, _store_distances(session), monkeypatch)
    return client.get(f"{WIDGET_URL}&suggest={suggest}").json()


def test_suggestions_rank_same_brand_first_then_price(
    db: tuple[Engine, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine, session = db
    _seed(session)
    _product(session, 1, "monster-ultra", "Monster", "Ultra")
    # Same brand ranks first even though it is not the cheapest suggestion.
    _product(session, 2, "monster-rose", "Monster", "Rose")
    _product(session, 3, "alpha-cheap", "Alpha", "Cheap", pack_count=4)
    _product(session, 4, "beta-tie", "Beta", "Tie")
    _product(session, 5, "alpha-dear", "Alpha", "Dear")
    _product(session, 6, "no-prices", "Gamma", "Nothing")
    _obs(session, 100, product_id=2, store_id=1, price_cents=480)
    _obs(session, 101, product_id=3, store_id=1, price_cents=300)
    _obs(session, 102, product_id=4, store_id=1, price_cents=300)
    _obs(session, 103, product_id=5, store_id=2, chain_id=2, price_cents=350)
    session.commit()

    body = _widget(session, monkeypatch)

    # Price ties keep the catalogue query's brand-then-name order; a candidate
    # with no price never appears.
    assert [item["slug"] for item in body["items"]] == [
        "monster-ultra",
        "monster-rose",
        "alpha-cheap",
        "beta-tie",
        "alpha-dear",
    ]
    assert [item["suggested"] for item in body["items"]] == [False, True, True, True, True]

    same_brand = body["items"][1]
    assert (same_brand["price_cents"], same_brand["price"]) == (480, "$4.80")
    assert (same_brand["store"], same_brand["chain"], same_brand["suburb"]) == (
        "SpudShed Wangara",
        "spudshed",
        "Wangara",
    )
    assert same_brand["distance_km"] == 1.0
    assert same_brand["source"] == "scrape" and same_brand["promo"] == "none"
    assert same_brand["label"] == "Monster Rose 500ml"
    # Per-can price is computed from the candidate's own pack size.
    assert body["items"][2]["per_can_cents"] == 75


def test_suggest_attributes_chain_prices_and_prefers_a_store_row(
    db: tuple[Engine, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine, session = db
    _seed(session)
    _product(session, 1, "monster-ultra", "Monster", "Ultra")
    # Online-only price: published per chain, attributed to every store in it.
    _product(session, 2, "woolies-online", "Woolworths", "Own Brand")
    # A shelf sighting that beats a cheaper chain-wide row for the same shop.
    _product(session, 3, "spud-shelf", "SpudShed", "Shelf")
    _obs(session, 100, product_id=2, chain_id=2, price_cents=250)
    _obs(session, 101, product_id=3, store_id=1, price_cents=260)
    _obs(session, 102, product_id=3, chain_id=1, price_cents=240)
    session.commit()

    body = _widget(session, monkeypatch)

    online = body["items"][1]
    assert (online["slug"], online["price_cents"]) == ("woolies-online", 250)
    assert online["price_scope"] == "chain"
    assert (online["store"], online["chain"], online["distance_km"]) == (
        "Woolworths Perth",
        "woolworths",
        2.5,
    )
    # Equal observation times, and latest_prices() breaks that tie in favour of
    # the row seen on the shelf, so the $2.40 online price for SpudShed does not
    # become the shelf price.
    shelf = body["items"][2]
    assert (shelf["slug"], shelf["price_cents"], shelf["store"]) == (
        "spud-shelf",
        260,
        "SpudShed Wangara",
    )
    assert shelf["price_scope"] == "store"


def test_suggestions_come_from_fresh_offers_not_the_first_60_alphabetically(
    db: tuple[Engine, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old pool was order_by(brand, name).limit(60), so V could never appear.

    Live evidence: a widget tracking V returned no V suggestion although 63
    fresh V prices existed nearby - the alphabetical cut-off ended inside the
    M's. The pool is now every verified drink with a fresh offer at a scanned
    store, ranked by brand match then price.
    """
    _engine, session = db
    _seed(session)
    _product(session, 1, "monster-ultra", "Monster", "Ultra")
    for index in range(70):
        _product(session, 100 + index, f"aardvark-{index:02d}", "Aardvark", f"Flavour {index}")
        _obs(session, 1000 + index, product_id=100 + index, store_id=1, price_cents=400 + index)
    _product(session, 200, "v-blue-500ml", "V", "Blue")
    _obs(session, 2000, product_id=200, store_id=1, price_cents=300)
    # Neither a pending product nor one with no fresh offer is part of the pool.
    pending = _product(session, 300, "monster-smoke-test-500ml", "Monster", "Smoke Test")
    pending.status = "pending"
    _obs(session, 3000, product_id=300, store_id=1, price_cents=100)
    _product(session, 301, "mystery-nothing", "Mystery", "Nothing")
    session.commit()

    body = _widget(session, monkeypatch, suggest=8)

    suggested = [item["slug"] for item in body["items"] if item["suggested"]]
    assert suggested[0] == "v-blue-500ml"
    assert len(suggested) == 8
    assert "monster-smoke-test-500ml" not in suggested
    assert "mystery-nothing" not in suggested


def test_suggest_reads_the_alias_group_and_the_newest_row(
    db: tuple[Engine, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine, session = db
    _seed(session)
    _product(session, 1, "monster-ultra", "Monster", "Ultra")
    _product(session, 2, "v-sugarfree", "V", "Sugarfree")
    # A retailer-specific spelling of the same can: same drink, separate row.
    _product(session, 3, "v-sugarfree-woolies", "V", "Sugarfree", canonical=2)
    _obs(session, 100, product_id=2, store_id=1, price_cents=900, hours_ago=6)
    _obs(session, 101, product_id=3, store_id=1, price_cents=320, hours_ago=5)
    _obs(session, 102, product_id=3, store_id=1, price_cents=280, hours_ago=1)
    session.commit()

    body = _widget(session, monkeypatch)

    # The label and slug come from the candidate, the price from whichever row
    # in its group was observed last - 280, not the alias's older 320 or the
    # candidate's own stale 900.
    suggestion = body["items"][1]
    assert (suggestion["slug"], suggestion["label"]) == ("v-sugarfree", "V Sugarfree 500ml")
    assert suggestion["price_cents"] == 280
    assert suggestion["age_hours"] == pytest.approx(1.0, abs=0.1)


def test_suggest_quotes_the_scope_latest_prices_would_pick(db: tuple[Engine, Session]) -> None:
    """Regression: a stale store row must not shadow a fresher chain scrape.

    ``_offers_for_product()`` renders ``latest_prices()``, which keeps whichever
    scope was observed more recently. The batched reduce used to keep the store
    row whenever one existed, so a 71-hour-old $3.90 crowd sighting became the
    suggestion while the tracked row for the same drink showed $3.10.
    """
    _engine, session = db
    _seed(session)
    _product(session, 1, "monster-ultra", "Monster", "Ultra")
    _product(session, 2, "v-sugarfree", "V", "Sugarfree")
    _obs(session, 100, product_id=2, store_id=1, price_cents=390, hours_ago=71)
    _obs(session, 101, product_id=2, chain_id=1, price_cents=310, hours_ago=8)
    session.commit()

    product = session.scalars(select(Product).where(Product.id == 2)).one()
    suggestion = public._suggest_offers(
        session, [product], _store_distances(session), NOW - timedelta(hours=72)
    )[2]

    # What the single-product path picks for this store, straight from the rule
    # it applies (latest_prices() is Postgres-only SQL and cannot run here).
    expected = _freshest(session.get(Observation, 100), session.get(Observation, 101), 1)
    assert expected is not None
    assert (suggestion.price_cents, suggestion.price_scope) == (
        expected.primary.price_cents,
        expected.primary.price_scope,
    )
    assert (suggestion.price_cents, suggestion.price_scope) == (310, "chain")
    # The chain row carries no store_id of its own; the offer must be stamped
    # with the shop it is attributed to.
    assert suggestion.store_id == 1


def test_suggest_keeps_a_store_row_observed_later_than_the_chain_row(
    db: tuple[Engine, Session],
) -> None:
    """Freshest still wins the other way: a newer shelf sighting beats the proxy."""
    _engine, session = db
    _seed(session)
    _product(session, 1, "monster-ultra", "Monster", "Ultra")
    _product(session, 2, "spud-shelf", "SpudShed", "Shelf")
    _obs(session, 100, product_id=2, store_id=1, price_cents=250, hours_ago=1)
    _obs(session, 101, product_id=2, chain_id=1, price_cents=240, hours_ago=6)
    session.commit()

    product = session.scalars(select(Product).where(Product.id == 2)).one()
    suggestion = public._suggest_offers(
        session, [product], _store_distances(session), NOW - timedelta(hours=72)
    )[2]

    assert (suggestion.price_cents, suggestion.price_scope) == (250, "store")


def test_suggest_price_lookup_is_two_queries_however_many_candidates(
    db: tuple[Engine, Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, session = db
    _seed(session)
    _product(session, 1, "monster-ultra", "Monster", "Ultra")
    for index in range(30):
        _product(session, 100 + index, f"candidate-{index}", f"Brand{index}", "Can")
        _obs(session, 1000 + index, product_id=100 + index, store_id=1, price_cents=300 + index)
    # Rows the suggestion query must not treat as prices.
    _product(session, 200, "rejected", "Zeta", "Rejected")
    _obs(session, 2000, product_id=200, store_id=1, price_cents=100, status="rejected")
    _product(session, 201, "stale", "Zeta", "Stale")
    _obs(session, 2001, product_id=201, store_id=1, price_cents=100, hours_ago=100)
    session.commit()

    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement.lower())

    client = _client(session, _store_distances(session), monkeypatch)
    statements.clear()
    body = client.get(f"{WIDGET_URL}&suggest=8").json()

    suggestions = [item for item in body["items"] if item["suggested"]]
    assert len(suggestions) == 8
    assert {item["slug"] for item in suggestions} == {f"candidate-{index}" for index in range(8)}
    # The whole candidate set shares one price query per scope. An N+1 would
    # make this 30+.
    assert len([sql for sql in statements if "from observations" in sql]) == 2
    assert len(statements) <= 6
