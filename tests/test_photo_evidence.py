"""Photo evidence on a sighting: returned, attributable and only the owner's.

The photo id was stored but no response exposed it, the admin list could not say
which sighting a photo backed, and attaching a token ignored who uploaded it.
These tests drive the real serialisers, the public feed, the admin listing and
the sighting write path against an in-memory SQLite database.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

import app.api.crowd as crowd_api
from app.api import admin, photos, public
from app.models import (
    Base,
    Chain,
    Device,
    Observation,
    ObservationVote,
    Photo,
    PriceDaily,
    Product,
    Store,
)
from app.schemas import ObservationIn

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)

# observations.id is a BigInteger primary key, which SQLite will not fill in for
# us; a mapper hook hands the write path's new rows an id, as in the other crowd
# test modules.
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
    Photo.__table__,
)


@pytest.fixture(autouse=True)
def _stub_daily_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Postgres-only price_daily upsert out of the SQLite fixture."""
    monkeypatch.setattr(crowd_api, "sync_daily_price", lambda *args, **kwargs: None)


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


@pytest.fixture()
def base(session: Session) -> Session:
    chain = Chain(code="bp", name="BP", kind="servo")
    session.add(chain)
    session.flush()
    session.add(Store(id=556, chain_id=chain.id, name="BP Canning Highway", is_active=True))
    session.add(
        Product(slug="monster-green-500ml", brand="Monster", name="Green", size_ml=500)
    )
    session.add(Device(id=7, token_hash="token-owner"))
    session.add(Device(id=8, token_hash="token-other"))
    session.flush()
    return session


def _photo(
    session: Session,
    photo_id: int,
    token: str,
    *,
    uploaded_by: int | None = 7,
    pruned_at: datetime | None = None,
) -> Photo:
    photo = Photo(
        id=photo_id,
        token=token,
        filename=f"2026/11/{token}.jpg",
        content_type="image/jpeg",
        bytes=1234,
        sha256=f"{photo_id:064d}",
        uploaded_by=uploaded_by,
        pruned_at=pruned_at,
    )
    session.add(photo)
    return photo


def _observation(
    session: Session,
    *,
    observation_id: int,
    photo: Photo | None = None,
    photo_url: str | None = None,
    observed_at: datetime = NOW,
) -> Observation:
    observation = Observation(
        id=observation_id,
        store_id=556,
        chain_id=session.scalar(select(Chain.id)),
        product_id=session.scalar(select(Product.id)),
        price_cents=399,
        source="crowd",
        price_scope="store",
        device_id=7,
        photo_id=photo.id if photo is not None else None,
        photo_url=photo_url,
        observed_at=observed_at,
        status="accepted",
    )
    session.add(observation)
    return observation


def test_an_observation_response_carries_its_photo(base: Session) -> None:
    photo = _photo(base, 1, "shelf-tag")
    observation = _observation(base, observation_id=1, photo=photo)
    base.flush()

    [out] = crowd_api._serialize(base, [observation])

    assert out.photo_token == "shelf-tag"
    assert out.photo_url == photos.photo_url("shelf-tag")


def test_a_legacy_photo_url_is_still_returned_without_a_token(base: Session) -> None:
    observation = _observation(
        base, observation_id=2, photo_url="https://example.test/tag.jpg"
    )
    base.flush()

    [out] = crowd_api._serialize(base, [observation])

    assert out.photo_token is None
    assert out.photo_url == "https://example.test/tag.jpg"


def test_the_public_feed_carries_the_photo(base: Session) -> None:
    photo = _photo(base, 1, "shelf-tag")
    _observation(base, observation_id=3, photo=photo)
    base.flush()

    [row] = public.observations(
        product=None, store_id=None, source=None, limit=10, session=base
    )

    assert row["photo_token"] == "shelf-tag"
    assert row["photo_url"] == photos.photo_url("shelf-tag")


def test_the_admin_photo_list_names_the_sighting_it_backs(base: Session) -> None:
    attached = _photo(base, 2, "attached")
    _photo(base, 1, "orphan")
    observation = _observation(base, observation_id=4, photo=attached)
    base.flush()

    rows = admin.recent_photos(limit=50, session=base)

    by_token = {row["token"]: row for row in rows}
    assert by_token["attached"]["observation_id"] == observation.id
    assert by_token["attached"]["product_id"] == observation.product_id
    assert by_token["attached"]["store_id"] == 556
    # An upload that no sighting uses yet is still listed, just unattributed.
    assert by_token["orphan"]["observation_id"] is None
    assert by_token["orphan"]["product_id"] is None
    assert by_token["orphan"]["store_id"] is None


def _payload(photo: Photo) -> ObservationIn:
    return ObservationIn(
        store_id=556,
        product_slug="monster-green-500ml",
        price_cents=399,
        photo_token=photo.token,
        observed_at=NOW,
    )


def test_a_sighting_attaches_the_device_own_photo(base: Session) -> None:
    photo = _photo(base, 1, "mine")
    base.flush()

    out = crowd_api._record_observation(_payload(photo), base.get(Device, 7), base)

    assert out.photo_token == "mine"
    assert base.scalar(select(Observation.photo_id)) == photo.id


def test_a_sighting_refuses_another_devices_photo(base: Session) -> None:
    photo = _photo(base, 1, "someone-elses", uploaded_by=8)
    base.flush()

    with pytest.raises(HTTPException) as caught:
        crowd_api._record_observation(_payload(photo), base.get(Device, 7), base)

    assert caught.value.status_code == 422
    assert caught.value.detail == "unknown photo token"


def test_a_sighting_refuses_a_pruned_photo(base: Session) -> None:
    photo = _photo(base, 1, "pruned", pruned_at=NOW)
    base.flush()

    with pytest.raises(HTTPException) as caught:
        crowd_api._record_observation(_payload(photo), base.get(Device, 7), base)

    assert caught.value.status_code == 422
    assert caught.value.detail == "unknown photo token"
