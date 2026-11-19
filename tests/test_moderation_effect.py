"""Offline tests for moderation decisions actually taking effect.

Three defects shared one root: a decision could be recorded without being acted
on. Votes on scraped rows were never refused, the tally read the session before
the vote being cast (``autoflush=False`` in app/db.py), and ``price_daily`` was
written once at insert so it never followed the status. The fixture below keeps
autoflush off for the same reason the app does, and stands in only for the
Postgres-only upsert in app/daily.py; the lookup, restore and delete paths run
as they do in production.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

import app.api.admin as admin_api
import app.api.crowd as crowd_api
import app.daily as daily
from app.daily import PerthTZ, sync_daily_price
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
from app.schemas import ObservationOut, VoteIn

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
DAY = NOW.astimezone(PerthTZ).date()

# observations.id is a BigInteger primary key, which SQLite will not fill in for
# us; tests always run in one process, so a module counter keeps ids unique.
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
def _stub_daily_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace only the Postgres-only upsert; sync_daily_price still runs.

    The real writer uses percentile_cont() and ON CONFLICT, which SQLite cannot
    parse. This stand-in writes the same day-scope row so the tests can see
    whether a decision reached the ledger.
    """

    def _record(
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
        day = daily.local_day(observed_at or datetime.now(PerthTZ))
        row = session.scalar(
            select(PriceDaily).where(
                PriceDaily.day == day,
                PriceDaily.chain_id == chain_id,
                PriceDaily.product_id == product_id,
                PriceDaily.store_id.is_(None)
                if store_id is None
                else PriceDaily.store_id == store_id,
            )
        )
        if row is None:
            row = PriceDaily(
                day=day, chain_id=chain_id, product_id=product_id, store_id=store_id
            )
            session.add(row)
        row.price_cents = price_cents
        row.was_price_cents = was_price_cents
        row.promo_kind = promo_kind
        row.source = source
        session.flush()

    monkeypatch.setattr(daily, "record_daily_price", _record)


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
    # Same session configuration as app/db.py: autoflush off, which is what let
    # the just-cast vote go missing from the tally.
    with Session(engine, autoflush=False, expire_on_commit=False) as db:
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


def _device(session: Session, name: str) -> Device:
    row = Device(token_hash=f"token-{name}")
    session.add(row)
    session.flush()
    return row


def _observation(
    session: Session,
    store: Store,
    product: Product,
    *,
    device: Device | None = None,
    price_cents: int = 350,
    status: str = "accepted",
    source: str = "crowd",
    observed_at: datetime = NOW,
) -> Observation:
    row = Observation(
        store_id=store.id,
        chain_id=store.chain_id,
        product_id=product.id,
        price_cents=price_cents,
        promo_kind="none",
        source=source,
        price_scope="store",
        device_id=device.id if device is not None else None,
        observed_at=observed_at,
        status=status,
    )
    session.add(row)
    session.flush()
    return row


def _daily_row(
    session: Session, store: Store, product: Product, day: date = DAY
) -> PriceDaily | None:
    """The store-scope row for the test's shop on the ledger day."""
    return session.scalar(
        select(PriceDaily).where(
            PriceDaily.day == day,
            PriceDaily.chain_id == store.chain_id,
            PriceDaily.product_id == product.id,
            PriceDaily.store_id == store.id,
        )
    )


def _chain_row(
    session: Session, store: Store, product: Product, day: date = DAY
) -> PriceDaily | None:
    return session.scalar(
        select(PriceDaily).where(
            PriceDaily.day == day,
            PriceDaily.chain_id == store.chain_id,
            PriceDaily.product_id == product.id,
            PriceDaily.store_id.is_(None),
        )
    )


def _vote(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    device: Device,
    observation_id: int,
    value: int,
) -> ObservationOut:
    monkeypatch.setattr(crowd_api, "current_device", lambda authorization, *, session: device)
    monkeypatch.setattr(crowd_api, "write_limiter", SimpleNamespace(check=lambda key: None))
    return crowd_api.vote(
        observation_id, VoteIn(vote=value), authorization="Bearer test", session=session
    )


def _set_status(session: Session, observation: Observation, status: str) -> dict:
    return admin_api.set_observation_status(
        observation.id, admin_api.StatusRequest(status=status), session=session
    )


# ---- 1. scraped prices are not up for a vote -------------------------------


def test_three_devices_cannot_vote_a_scraped_price_away(
    session: Session, store: Store, product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The evidence case: an accepted Woolworths/Coles row has device_id NULL, so
    # the own-row guard never fired and three throwaway devices moved a real
    # price to needs_review, which every price surface filters out.
    scraped = _observation(session, store, product, device=None, source="scrape")
    daily.record_daily_price(
        session,
        product_id=product.id,
        chain_id=store.chain_id,
        store_id=store.id,
        price_cents=scraped.price_cents,
        source="scrape",
        observed_at=NOW,
    )
    voters = [_device(session, name) for name in ("a", "b", "c")]

    for device in voters:
        with pytest.raises(HTTPException) as caught:
            _vote(session, monkeypatch, device, scraped.id, -1)
        assert caught.value.status_code == 409

    session.refresh(scraped)
    assert scraped.status == "accepted"
    assert session.scalar(select(func.count()).select_from(ObservationVote)) == 0
    # And the price everyone else reads is still in the day's ledger.
    assert _daily_row(session, store, product).price_cents == 350


# ---- 2. the vote being cast decides the tally it reports -------------------


def test_the_second_confirm_accepts_and_counts_itself(
    session: Session, store: Store, product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    author, first, second = (_device(session, name) for name in ("author", "first", "second"))
    flagged = _observation(
        session, store, product, device=author, status="needs_review",
        observed_at=NOW - timedelta(hours=1),
    )
    # A flagged row writes no ledger row at insert, so approval is the only way
    # it can ever buy a deal badge.
    assert _daily_row(session, store, product) is None

    one = _vote(session, monkeypatch, first, flagged.id, 1)
    assert (one.status, one.confirms) == ("needs_review", 1)

    two = _vote(session, monkeypatch, second, flagged.id, 1)
    assert (two.status, two.confirms) == ("accepted", 2)

    daily_row = _daily_row(session, store, product)
    assert daily_row is not None
    assert (daily_row.price_cents, daily_row.source) == (350, "crowd")


def test_the_second_dispute_keeps_the_price_and_the_third_demotes(
    session: Session, store: Store, product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    author = _device(session, "author")
    accepted = _observation(session, store, product, device=author)
    daily.record_daily_price(
        session,
        product_id=product.id,
        chain_id=store.chain_id,
        store_id=store.id,
        price_cents=accepted.price_cents,
        source="crowd",
        observed_at=NOW,
    )
    # The chain row an aggregate scrape leaves behind cannot share the day with
    # this store row here: model-created tables still key on (day, chain,
    # product) until migration 012, so the aggregate has its own test below.
    session.commit()

    # Disputes accumulate one at a time: the second leaves the price in place,
    # the third is the decision that flags it and withdraws the row.
    first, second, third = (_device(session, name) for name in ("a", "b", "c"))
    one = _vote(session, monkeypatch, first, accepted.id, -1)
    assert (one.status, one.disputes) == ("accepted", 1)
    two = _vote(session, monkeypatch, second, accepted.id, -1)
    assert (two.status, two.disputes) == ("accepted", 2)
    assert _daily_row(session, store, product) is not None

    three = _vote(session, monkeypatch, third, accepted.id, -1)
    assert (three.status, three.disputes) == ("needs_review", 3)
    assert _daily_row(session, store, product) is None


def test_the_chain_aggregate_leaves_with_its_last_store_row(
    session: Session, store: Store, product: Product
) -> None:
    # _AGGREGATE_SQL only writes when store rows remain, so without the delete
    # the day's chain row would keep feeding the deal median a price nobody
    # stands behind.
    session.add(
        PriceDaily(
            day=DAY, chain_id=store.chain_id, product_id=product.id, store_id=None,
            price_cents=350, promo_kind="none", source="aggregate",
        )
    )
    session.flush()

    daily._refresh_chain_row(session, DAY, store.chain_id, product.id)

    assert _chain_row(session, store, product) is None


# ---- 3. price_daily follows the status ------------------------------------


def test_approving_a_row_writes_the_day_and_rejecting_removes_it(
    session: Session, store: Store, product: Product
) -> None:
    flagged = _observation(session, store, product, status="needs_review")

    assert _set_status(session, flagged, "accepted") == {"id": flagged.id, "status": "accepted"}
    daily_row = _daily_row(session, store, product)
    assert daily_row is not None
    assert (daily_row.price_cents, daily_row.source) == (350, "crowd")

    _set_status(session, flagged, "rejected")
    assert _daily_row(session, store, product) is None


def test_re_approving_an_accepted_row_repairs_a_missing_ledger_row(
    session: Session, store: Store, product: Product
) -> None:
    # Rows accepted before the ledger followed the status are absent from
    # price_daily; the approve action is the moderator's way to repair one.
    accepted = _observation(session, store, product)
    assert _daily_row(session, store, product) is None

    _set_status(session, accepted, "accepted")

    assert _daily_row(session, store, product).price_cents == 350


def test_rejecting_the_later_price_restores_the_one_still_accepted(
    session: Session, store: Store, product: Product
) -> None:
    earlier = _observation(
        session, store, product, price_cents=349, observed_at=NOW - timedelta(hours=1)
    )
    later = _observation(session, store, product, price_cents=399)
    daily.record_daily_price(
        session,
        product_id=product.id,
        chain_id=store.chain_id,
        store_id=store.id,
        price_cents=later.price_cents,
        source="crowd",
        observed_at=NOW,
    )

    _set_status(session, later, "rejected")

    daily_row = _daily_row(session, store, product)
    assert (daily_row.price_cents, daily_row.source) == (earlier.price_cents, "crowd")


def test_a_later_scrapes_row_is_not_withdrawn(
    session: Session, store: Store, product: Product
) -> None:
    # Last writer of the day wins, so a crowd row can sit under a scrape that
    # came after it. Rejecting the crowd row must not delete the scrape's price.
    crowd_row = _observation(session, store, product, price_cents=350)
    session.add(
        PriceDaily(
            day=DAY, chain_id=store.chain_id, product_id=product.id, store_id=store.id,
            price_cents=349, promo_kind="none", source="scrape",
        )
    )
    session.flush()

    _set_status(session, crowd_row, "rejected")

    assert _daily_row(session, store, product).price_cents == 349


def test_sync_is_a_no_op_when_the_day_has_no_row_for_this_scope(
    session: Session, store: Store, product: Product
) -> None:
    pending = _observation(session, store, product, status="needs_review")

    sync_daily_price(session, pending)

    assert _daily_row(session, store, product) is None
