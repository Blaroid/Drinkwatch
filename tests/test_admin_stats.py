"""Offline tests for the admin stats and moderation queues.

Both endpoints aggregate across tables, and both used the same shortcut of
joining everything onto one query. The store/observation multiplication in
``/v1/admin/stats`` was only visible on a database with more than one store per
chain, so the fixtures here deliberately give the busiest chain two stores.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.api.admin as admin_api
from app.models import (
    Base,
    Chain,
    Device,
    Listing,
    Observation,
    Photo,
    Product,
    Report,
    ScrapeRun,
    Store,
)

TABLES = (
    Chain.__table__,
    Store.__table__,
    Product.__table__,
    Device.__table__,
    Observation.__table__,
    Photo.__table__,
    Report.__table__,
    Listing.__table__,
    ScrapeRun.__table__,
)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=TABLES)
    with Session(engine, expire_on_commit=False) as db:
        yield db


@pytest.fixture()
def catalogue(session: Session) -> dict[str, object]:
    spudshed = Chain(code="spudshed", name="SpudShed")
    coles = Chain(code="coles", name="Coles")
    iga = Chain(code="iga", name="IGA")
    session.add_all([spudshed, coles, iga])
    session.flush()

    product = Product(
        slug="monster-ultra-500ml",
        brand="Monster",
        name="Ultra",
        size_ml=500,
        pack_count=1,
        category="energy",
    )
    session.add(product)
    session.flush()
    return {"spudshed": spudshed, "coles": coles, "iga": iga, "product": product}


def test_per_chain_observations_are_not_multiplied_by_stores(
    session: Session, catalogue: dict[str, object]
) -> None:
    """One observation beside two stores must count once.

    The old query joined Store and Observation onto Chain in the same query, so
    the single row was emitted once per store (2) and ``per_chain`` summed to
    more than the observation table held.
    """
    spudshed = catalogue["spudshed"]
    coles = catalogue["coles"]
    product = catalogue["product"]
    session.add_all(
        [
            Store(chain_id=spudshed.id, name="SpudShed Kelmscott"),
            Store(chain_id=spudshed.id, name="SpudShed Waterford"),
            Store(chain_id=coles.id, name="Coles Victoria Park"),
        ]
    )
    session.flush()
    store_ids = list(session.scalars(select(Store.id).order_by(Store.id)))
    session.add_all(
        [
            Observation(
                id=1,
                store_id=store_ids[0],
                chain_id=spudshed.id,
                product_id=product.id,
                price_cents=399,
                source="scrape",
                price_scope="store",
            ),
            Observation(
                id=2,
                store_id=store_ids[2],
                chain_id=coles.id,
                product_id=product.id,
                price_cents=450,
                source="scrape",
                price_scope="store",
            ),
        ]
    )
    session.commit()

    result = admin_api.stats(session=session)
    per_chain = {row["chain"]: row for row in result["per_chain"]}

    assert per_chain["spudshed"]["stores"] == 2
    assert per_chain["spudshed"]["observations"] == 1
    assert per_chain["coles"]["observations"] == 1
    assert per_chain["iga"] == {"chain": "iga", "stores": 0, "observations": 0}
    assert result["counts"]["observations"] == 2
    assert sum(row["observations"] for row in result["per_chain"]) == 2


def test_chain_scope_observation_counts_once_without_a_store(
    session: Session, catalogue: dict[str, object]
) -> None:
    """A chain-wide row has no store to multiply it, and still belongs to the chain."""
    spudshed = catalogue["spudshed"]
    product = catalogue["product"]
    session.add_all(
        [
            Store(chain_id=spudshed.id, name="SpudShed Kelmscott"),
            Store(chain_id=spudshed.id, name="SpudShed Waterford"),
            Observation(
                id=1,
                store_id=None,
                chain_id=spudshed.id,
                product_id=product.id,
                price_cents=399,
                source="scrape",
                price_scope="chain",
            ),
        ]
    )
    session.commit()

    result = admin_api.stats(session=session)
    per_chain = {row["chain"]: row for row in result["per_chain"]}
    assert per_chain["spudshed"]["stores"] == 2
    assert per_chain["spudshed"]["observations"] == 1


def test_pending_products_carry_their_crowd_sightings(
    session: Session, catalogue: dict[str, object]
) -> None:
    """The queue has to show the evidence, not just a count."""
    coles = catalogue["coles"]
    store = Store(chain_id=coles.id, name="Coles Victoria Park", suburb="Victoria Park")
    session.add(store)
    session.flush()
    product = Product(
        slug="flying-power-just-peachy-50ml",
        brand="Flying Power",
        name="Just Peachy",
        size_ml=50,
        pack_count=1,
        status="pending",
        source="crowd",
        barcode="4434835497265",
    )
    device = Device(token_hash="a" * 64, platform="android", app_version="0.4.0")
    session.add_all([product, device])
    session.flush()
    photo = Photo(
        id=1,
        token="photo-token",
        filename="2026/11/photo-token.jpg",
        content_type="image/jpeg",
        bytes=293,
        sha256="b" * 64,
        width=32,
        height=32,
        uploaded_by=device.id,
    )
    session.add(photo)
    session.flush()
    session.add(
        Observation(
            id=24066,
            store_id=store.id,
            chain_id=coles.id,
            product_id=product.id,
            price_cents=50000,
            source="crowd",
            price_scope="store",
            device_id=device.id,
            photo_id=photo.id,
            status="needs_review",
            observed_at=datetime(2026, 11, 19, 3, 26, tzinfo=timezone.utc),
        )
    )
    session.commit()

    queue = admin_api.pending_products(limit=100, session=session)
    assert len(queue) == 1
    assert queue[0]["observations"] == 1
    sighting = queue[0]["sightings"][0]
    assert sighting["id"] == 24066
    assert sighting["status"] == "needs_review"
    assert sighting["price_cents"] == 50000
    assert sighting["store"] == "Coles Victoria Park"
    assert sighting["photo_token"] == "photo-token"

    flagged = admin_api.observations(status_filter="needs_review", limit=100, session=session)
    assert [row["id"] for row in flagged] == [24066]
    assert flagged[0]["product_slug"] == "flying-power-just-peachy-50ml"
    assert flagged[0]["photo_token"] == "photo-token"
