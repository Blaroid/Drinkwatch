"""Offline tests for the crowd write quota on the receipt batch endpoint.

A receipt is one act of reporting, so the batch path charges the hour's limiter
once for the whole docket instead of once per line. These tests drive the real
handlers against the real RateLimiter; an in-memory SQLite database with only
the tables the write path touches is enough, since the batch never reaches the
Postgres-only parts (the daily ledger is stubbed, as in the folding tests).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

import app.api.crowd as crowd_api
from app.models import (
    Base,
    Chain,
    Device,
    Observation,
    ObservationVote,
    PriceDaily,
    Product,
    Store,
)
from app.schemas import BatchObservationIn, ObservationIn
from app.security import RateLimiter

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
    PriceDaily.__table__,
)


@pytest.fixture(autouse=True)
def _stub_daily_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Postgres-only price_daily upsert out of the SQLite fixture."""
    monkeypatch.setattr(crowd_api, "sync_daily_price", lambda *args, **kwargs: None)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    # The vote tally that _serialize_one() runs sums greatest()/least(); SQLite
    # has only max()/min(), so teach the test connection the missing two.
    @event.listens_for(engine, "connect")
    def _register_functions(dbapi_connection, _record) -> None:  # pragma: no cover
        dbapi_connection.create_function("greatest", 2, max)
        dbapi_connection.create_function("least", 2, min)

    Base.metadata.create_all(engine, tables=TABLES)
    with Session(engine, expire_on_commit=False) as db:
        yield db


@pytest.fixture()
def store(session: Session) -> Store:
    chain = Chain(code="bp", name="BP", kind="servo")
    session.add(chain)
    session.flush()
    row = Store(chain_id=chain.id, name="BP Canning Highway", is_active=True)
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def product(session: Session) -> Product:
    row = Product(slug="monster-green-500ml", brand="Monster", name="Green", size_ml=500)
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def device(session: Session, monkeypatch: pytest.MonkeyPatch) -> Device:
    row = Device(token_hash="token-batch")
    session.add(row)
    session.flush()
    monkeypatch.setattr(
        crowd_api, "current_device", lambda authorization, *, session: row
    )
    return row


def _items(store: Store, product: Product, count: int, start_cents: int = 400) -> list[ObservationIn]:
    """One docket: distinct prices, so every line is a new sighting."""
    return [
        ObservationIn(
            store_id=store.id,
            product_slug=product.slug,
            price_cents=start_cents + index,
            observed_at=NOW,
        )
        for index in range(count)
    ]


def _submit_batch(session: Session, items: list[ObservationIn]) -> list:
    return crowd_api.submit_batch(
        BatchObservationIn(items=items), authorization="Bearer test", session=session
    )


def _observation_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Observation)) or 0


def test_a_batch_charges_the_hour_once_not_once_per_line(
    session: Session, store: Store, product: Product, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 21 slots: the docket of 20 fits, and one ordinary crowd write must still
    # be possible afterwards. Charged per line, the docket alone would already
    # have taken 20 of the 21 and the single write would be the 22nd.
    monkeypatch.setattr(crowd_api, "write_limiter", RateLimiter(limit=21, window_seconds=3600))

    results = _submit_batch(session, _items(store, product, 20))

    assert [item.status for item in results] == ["accepted"] * 20
    assert _observation_count(session) == 20

    single = crowd_api.submit_observation(
        ObservationIn(store_id=store.id, product_slug=product.slug, price_cents=450, observed_at=NOW),
        authorization="Bearer test",
        session=session,
    )
    assert single.id is not None

    # The hour is now spent: a second docket is refused as a whole rather than
    # accepted line by line until it runs dry.
    with pytest.raises(HTTPException) as caught:
        _submit_batch(session, _items(store, product, 20, start_cents=500))
    assert caught.value.status_code == 429


def test_a_batch_that_does_not_fit_costs_nothing(
    session: Session, store: Store, product: Product, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crowd_api, "write_limiter", RateLimiter(limit=20, window_seconds=3600))
    _submit_batch(session, _items(store, product, 20))
    before = _observation_count(session)

    # One line over the limit: the charge is atomic, so the second docket must
    # be refused before it writes anything the client would have to resubmit
    # around.
    with pytest.raises(HTTPException) as caught:
        _submit_batch(session, _items(store, product, 1, start_cents=900))

    assert caught.value.status_code == 429
    assert _observation_count(session) == before


def test_a_single_sighting_still_charges_one_slot(
    session: Session, store: Store, product: Product, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crowd_api, "write_limiter", RateLimiter(limit=1, window_seconds=3600))
    payload = ObservationIn(
        store_id=store.id, product_slug=product.slug, price_cents=400, observed_at=NOW
    )

    crowd_api.submit_observation(payload, authorization="Bearer test", session=session)

    with pytest.raises(HTTPException) as caught:
        crowd_api.submit_observation(
            payload.model_copy(update={"price_cents": 401}),
            authorization="Bearer test",
            session=session,
        )
    assert caught.value.status_code == 429


def test_a_bad_line_fails_only_itself(
    session: Session, store: Store, product: Product, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The batch is charged up front, but validation is still per item: a receipt
    # line the catalogue cannot match must not cost the user the rest of the
    # docket.
    monkeypatch.setattr(crowd_api, "write_limiter", RateLimiter(limit=60, window_seconds=3600))
    items = _items(store, product, 3)
    items[1] = items[1].model_copy(update={"store_id": 9999})

    results = _submit_batch(session, items)

    assert [item.status for item in results] == ["accepted", "error", "accepted"]
    assert results[1].error == "unknown store"
    assert _observation_count(session) == 2
