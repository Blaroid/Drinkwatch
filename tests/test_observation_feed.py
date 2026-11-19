"""The sightings feed must not launder a quarantined price as an ordinary sighting.

/v1/observations filtered only ``status != 'rejected'`` and had no ``status``
key, so a ``needs_review`` outlier was served as a normal row while every price
endpoint refused to quote it (live case: a $500.00 crowd sighting in the feed,
``offers: []`` on /v1/prices). These tests pin the feed to accepted rows, the
``status`` each row now carries, and the opt-in that still exposes flagged rows
for a client that wants to badge them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import public
from app.models import Base, Chain, Observation, Product, Store

# SQLite drops tzinfo on DateTime(timezone=True) columns, so the fixture uses
# naive UTC instants and the feed's rows all compare cleanly.
NOW = datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[Chain.__table__, Store.__table__, Product.__table__, Observation.__table__],
    )
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def _store(session: Session) -> Store:
    chain = Chain(id=1, code="spudshed", name="SpudShed", kind="independent")
    store = Store(id=1, chain_id=1, name="SpudShed Wangara", suburb="Wangara")
    session.add_all([chain, store])
    product = Product(
        id=1,
        slug="monster-green-500ml",
        brand="Monster",
        name="Green",
        size_ml=500,
        category="energy",
        is_active=True,
    )
    session.add(product)
    return store


def _observation(
    session: Session,
    obs_id: int,
    *,
    price_cents: int,
    status: str,
    source: str = "crowd",
    minutes_ago: int = 1,
) -> Observation:
    row = Observation(
        id=obs_id,
        store_id=1,
        chain_id=1,
        product_id=1,
        price_cents=price_cents,
        source=source,
        price_scope="store",
        status=status,
        observed_at=NOW - timedelta(minutes=minutes_ago),
    )
    session.add(row)
    return row


def test_feed_omits_needs_review_rows_and_says_what_each_row_is(session: Session) -> None:
    _store(session)
    accepted = _observation(session, 100, price_cents=350, status="accepted")
    _observation(session, 101, price_cents=50000, status="needs_review", minutes_ago=2)
    session.commit()

    body = public.observations(
        product=None, store_id=None, source=None, include_flagged=False, limit=50, session=session
    )

    # The quarantined $500.00 row is not a sighting the app should render; the
    # accepted row now declares its status instead of leaving the client to
    # assume every row is good.
    assert [row["id"] for row in body] == [accepted.id]
    assert body[0]["status"] == "accepted"
    assert body[0]["price_cents"] == 350


def test_flagged_rows_can_be_asked_for_and_keep_their_status(session: Session) -> None:
    _store(session)
    _observation(session, 100, price_cents=350, status="accepted")
    flagged = _observation(session, 101, price_cents=50000, status="needs_review", minutes_ago=2)
    session.commit()

    body = public.observations(
        product=None, store_id=None, source=None, include_flagged=True, limit=50, session=session
    )

    by_id = {row["id"]: row for row in body}
    assert set(by_id) == {100, flagged.id}
    assert by_id[flagged.id]["status"] == "needs_review"
    # The price is still exposed, but now labelled: a client can badge it
    # instead of mistaking it for an accepted sighting.
    assert by_id[flagged.id]["price_cents"] == 50000


def test_rejected_rows_never_appear_even_when_flagged_are_requested(session: Session) -> None:
    _store(session)
    _observation(session, 100, price_cents=350, status="accepted")
    _observation(session, 101, price_cents=999, status="rejected", minutes_ago=2)
    session.commit()

    body = public.observations(
        product=None, store_id=None, source=None, include_flagged=True, limit=50, session=session
    )

    assert [row["id"] for row in body] == [100]
