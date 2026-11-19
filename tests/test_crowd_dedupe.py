"""Offline tests for folding duplicate crowd sightings.

The fold is a pure function over Observation rows and the tally is plain
SQLAlchemy, so an in-memory SQLite database with the tables those touch is
enough. No network, no Postgres, no live catalogue.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import app.api.crowd as crowd_api
from app.api.crowd import SIGHTING_FOLD_WINDOW, _sightings_in_window, fold_crowd_sightings
from app.api.deps import utcnow
from app.api.public import observations as recent_sightings
from app.models import (
    Base,
    Chain,
    Device,
    Observation,
    ObservationVote,
    Product,
    Store,
)
from app.schemas import ObservationIn, ObservationOut

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)

# observations.id is a BigInteger primary key, which SQLite will not fill in for
# us; tests always run in one process, so a module counter keeps ids unique and
# a mapper hook hands one to rows the endpoint inserts itself.
_OBSERVATION_IDS = itertools.count(1)


@event.listens_for(Observation, "before_insert")
def _assign_observation_id(_mapper, _connection, target: Observation) -> None:
    if target.id is None:
        target.id = next(_OBSERVATION_IDS)

TABLES = (
    Chain.__table__,
    Store.__table__,
    Product.__table__,
    Device.__table__,
    Observation.__table__,
    ObservationVote.__table__,
)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    # The vote tally that _serialize() runs sums greatest()/least(); SQLite has
    # only max()/min(), so teach the test connection the missing two.
    @event.listens_for(engine, "connect")
    def _register_functions(dbapi_connection, _record) -> None:  # pragma: no cover
        dbapi_connection.create_function("greatest", 2, max)
        dbapi_connection.create_function("least", 2, min)

    Base.metadata.create_all(engine, tables=TABLES)
    with Session(engine, expire_on_commit=False) as db:
        yield db


def _chain(db: Session) -> Chain:
    chain = Chain(code="bp", name="BP", kind="servo")
    db.add(chain)
    db.flush()
    return chain


def _store(db: Session, chain: Chain) -> Store:
    store = Store(chain_id=chain.id, name="BP Canning Highway", is_active=True)
    db.add(store)
    db.flush()
    return store


def _product(db: Session) -> Product:
    product = Product(slug="monster-green-500ml", brand="Monster", name="Green", size_ml=500)
    db.add(product)
    db.flush()
    return product


def _row(
    *,
    store_id: int = 1,
    product_id: int = 1,
    price_cents: int = 350,
    observed_at: datetime = NOW,
    source: str = "crowd",
    status: str = "accepted",
) -> Observation:
    """A detached Observation, for the pure fold: nothing is loaded lazily."""
    return Observation(
        id=next(_OBSERVATION_IDS),
        store_id=store_id,
        chain_id=1,
        product_id=product_id,
        price_cents=price_cents,
        promo_kind="none",
        source=source,
        price_scope="store",
        observed_at=observed_at,
        status=status,
    )


# ---- the fold ---------------------------------------------------------------


def test_five_reporters_of_one_price_are_one_sighting() -> None:
    reports = [
        _row(observed_at=NOW - timedelta(minutes=minutes)) for minutes in (1, 5, 10, 20, 40)
    ]

    folded = fold_crowd_sightings(reports)

    assert len(folded) == 1
    newest, sightings = folded[0]
    assert newest.observed_at == NOW - timedelta(minutes=1)
    assert sightings == 5


def test_reports_an_hour_apart_stay_separate() -> None:
    # The window ends at the newest report of a group, so a report exactly one
    # hour older is already a different fact rather than the same one again.
    reports = [
        _row(observed_at=NOW),
        _row(observed_at=NOW - timedelta(minutes=30)),
        _row(observed_at=NOW - SIGHTING_FOLD_WINDOW),
    ]

    folded = fold_crowd_sightings(reports)

    assert [(row.observed_at, count) for row, count in folded] == [
        (NOW, 2),
        (NOW - SIGHTING_FOLD_WINDOW, 1),
    ]


def test_a_different_price_store_or_product_is_not_folded() -> None:
    reports = [
        _row(price_cents=350),
        _row(price_cents=399),
        _row(price_cents=350, store_id=2),
        _row(price_cents=350, product_id=2),
    ]

    folded = fold_crowd_sightings(reports)

    assert [count for _, count in folded] == [1, 1, 1, 1]


def test_scrape_rows_are_never_folded() -> None:
    # A scraper writes one row per price change, so two scrape rows at the same
    # price are two real observations and the feed exists to show them.
    reports = [
        _row(observed_at=NOW - timedelta(minutes=10), source="scrape"),
        _row(observed_at=NOW - timedelta(minutes=20), source="scrape"),
        _row(observed_at=NOW),
    ]

    folded = fold_crowd_sightings(reports)

    assert [(row.source, count) for row, count in folded] == [
        ("crowd", 1),
        ("scrape", 1),
        ("scrape", 1),
    ]


def test_folding_returns_newest_first_even_for_unordered_input() -> None:
    oldest = _row(observed_at=NOW - timedelta(minutes=20), price_cents=429)
    middle = _row(observed_at=NOW - timedelta(minutes=10), price_cents=399)
    newest = _row(observed_at=NOW)

    folded = fold_crowd_sightings([oldest, newest, middle])

    assert [row.observed_at for row, _ in folded] == [
        newest.observed_at,
        middle.observed_at,
        oldest.observed_at,
    ]


# ---- the tally --------------------------------------------------------------


def test_tally_counts_only_agreeing_reports_in_the_hour(session: Session) -> None:
    chain = _chain(session)
    store = _store(session, chain)
    product = _product(session)
    anchor = _row(store_id=store.id, product_id=product.id, observed_at=NOW)
    session.add(anchor)
    session.flush()
    session.add_all(
        [
            _row(store_id=store.id, product_id=product.id, observed_at=NOW - timedelta(minutes=10)),
            _row(
                store_id=store.id,
                product_id=product.id,
                observed_at=NOW - timedelta(minutes=20),
                status="needs_review",
            ),
            # Same price but rejected: we already decided it was wrong.
            _row(
                store_id=store.id,
                product_id=product.id,
                observed_at=NOW - timedelta(minutes=10),
                status="rejected",
            ),
            # Another price, another shop, and later than the report itself.
            _row(
                store_id=store.id,
                product_id=product.id,
                price_cents=399,
                observed_at=NOW - timedelta(minutes=10),
            ),
            _row(store_id=2, product_id=product.id, observed_at=NOW - timedelta(minutes=10)),
            _row(store_id=store.id, product_id=product.id, observed_at=NOW + timedelta(minutes=5)),
            # A report older than the window is history, not corroboration.
            _row(
                store_id=store.id,
                product_id=product.id,
                observed_at=NOW - SIGHTING_FOLD_WINDOW - timedelta(minutes=1),
            ),
        ]
    )
    session.flush()

    assert _sightings_in_window(session, anchor) == 3


def test_tally_is_one_for_a_report_nobody_else_made(session: Session) -> None:
    store = _store(session, _chain(session))
    product = _product(session)
    anchor = _row(store_id=store.id, product_id=product.id, observed_at=NOW)
    session.add(anchor)
    session.flush()

    assert _sightings_in_window(session, anchor) == 1


def test_observation_out_reports_one_sighting_by_default() -> None:
    # The response model must fill `sightings` for rows built before the field
    # existed, so old rows keep validating and clients keep getting a number.
    item = ObservationOut.model_validate(_row())

    assert item.sightings == 1


# ---- the write path, end to end --------------------------------------------


def test_five_devices_reporting_one_price_become_five_sightings(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = _chain(session)
    store = _store(session, chain)
    product = _product(session)
    devices = [Device(token_hash=f"token-{index}") for index in range(5)]
    session.add_all(devices)
    session.flush()
    # One price, one shop, five people: the ledger keeps five rows, the API
    # keeps one sighting with a count.
    observed_at = utcnow() - timedelta(minutes=10)
    monkeypatch.setattr(crowd_api, "write_limiter", SimpleNamespace(check=lambda key: None))

    saved: list[ObservationOut] = []
    for device in devices:
        monkeypatch.setattr(
            crowd_api,
            "current_device",
            lambda authorization, *, session, device=device: device,
        )
        saved.append(
            crowd_api.submit_observation(
                ObservationIn(
                    store_id=store.id,
                    product_slug=product.slug,
                    price_cents=350,
                    observed_at=observed_at,
                ),
                authorization="Bearer test",
                session=session,
            )
        )

    assert [item.sightings for item in saved] == [1, 2, 3, 4, 5]
    assert len({item.id for item in saved}) == 5

    # The same device reporting again is the same sighting (201, same id), and
    # its response still carries the full tally instead of resetting to one.
    monkeypatch.setattr(
        crowd_api, "current_device", lambda authorization, *, session: devices[0]
    )
    repeat = crowd_api.submit_observation(
        ObservationIn(
            store_id=store.id,
            product_slug=product.slug,
            price_cents=350,
            observed_at=observed_at,
        ),
        authorization="Bearer test",
        session=session,
    )
    assert repeat.id == saved[0].id
    assert repeat.sightings == 5

    # And the public feed shows one row saying "5 sightings" rather than five
    # identical rows competing to be the latest.
    feed = recent_sightings(
        product=None, store_id=store.id, source="crowd", limit=50, session=session
    )
    assert len(feed) == 1
    assert feed[0]["price_cents"] == 350
    assert feed[0]["sightings"] == 5
