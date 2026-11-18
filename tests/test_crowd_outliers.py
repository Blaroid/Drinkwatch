"""Offline tests for the crowd outlier reference.

The reference query is plain SQLAlchemy, so an in-memory SQLite database with
only the tables the check touches is enough -- the Postgres-only JSONB columns
live in tables this module never creates. No network, no Postgres, no fixtures
from the live catalogue.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.crowd import _Reference, _reference_price
from app.models import Base, Chain, Device, Observation, Product, Store

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)

# observations.id is a BigInteger primary key, which SQLite will not fill in for
# us; tests always run in one process, so a module counter keeps ids unique.
_OBSERVATION_IDS = itertools.count(1)

TABLES = (
    Chain.__table__,
    Store.__table__,
    Product.__table__,
    Device.__table__,
    Observation.__table__,
)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=TABLES)
    with Session(engine, expire_on_commit=False) as db:
        yield db


def _chain(db: Session, code: str, kind: str) -> Chain:
    chain = Chain(code=code, name=code.title(), kind=kind)
    db.add(chain)
    db.flush()
    return chain


def _store(db: Session, chain: Chain, name: str) -> Store:
    store = Store(chain_id=chain.id, name=name, is_active=True)
    db.add(store)
    db.flush()
    return store


def _product(db: Session, slug: str = "monster-green-500ml") -> Product:
    product = Product(slug=slug, brand="Monster", name="Green", size_ml=500, pack_count=1)
    db.add(product)
    db.flush()
    return product


def _device(db: Session, name: str) -> Device:
    device = Device(token_hash=f"hash-{name}", display_name=name)
    db.add(device)
    db.flush()
    return device


def _observe(
    db: Session,
    *,
    store: Store,
    product: Product,
    price_cents: int,
    device: Device | None = None,
    hours_ago: float = 1,
    status: str = "accepted",
) -> Observation:
    observation = Observation(
        id=next(_OBSERVATION_IDS),
        store_id=store.id,
        chain_id=store.chain_id,
        product_id=product.id,
        price_cents=price_cents,
        source="crowd" if device is not None else "scrape",
        price_scope="store",
        device_id=device.id if device is not None else None,
        observed_at=NOW - timedelta(hours=hours_ago),
        status=status,
    )
    db.add(observation)
    db.flush()
    return observation


def _seed_chain(db: Session, code: str, kind: str, prices: list[int], product: Product) -> Store:
    chain = _chain(db, code, kind)
    store = _store(db, chain, f"{code} store")
    for index, price in enumerate(prices):
        _observe(db, store=store, product=product, price_cents=price, hours_ago=index + 1)
    return store


def test_reference_uses_the_submitting_chain_not_every_chain(session: Session) -> None:
    product = _product(session)
    _seed_chain(session, "woolworths", "supermarket", [400, 410, 420, 430], product)
    seven_store = _seed_chain(session, "7eleven", "convenience", [1100, 1090, 1110, 1105], product)

    reference = _reference_price(session, product, seven_store, device_id=None)

    assert reference is not None
    # Judged against its own chain, the convenience price is ordinary. Against
    # the all-chain median of the old code it would be ~2.7x and held for review.
    assert reference.median_cents == pytest.approx(1102.5)
    assert reference.samples == 4
    assert not reference.is_outlier(1100)
    assert reference.is_outlier(400)


def test_reference_widens_to_same_chain_kind_when_chain_is_thin(session: Session) -> None:
    product = _product(session)
    _seed_chain(session, "woolworths", "supermarket", [400, 410, 420, 430], product)
    _seed_chain(session, "bp", "servo", [900, 910, 905, 895], product)
    _seed_chain(session, "vibe", "convenience", [1080, 1100, 1120], product)
    seven_store = _seed_chain(session, "7eleven", "convenience", [1110], product)

    reference = _reference_price(session, product, seven_store, device_id=None)

    assert reference is not None
    # 7-Eleven's single row is not a population, so the convenience kind steps in.
    # Servo and supermarket prices are not part of the comparison.
    assert reference.samples == 4
    assert 1080 <= reference.median_cents <= 1120
    assert reference.is_outlier(400)
    assert not reference.is_outlier(1110)


def test_reference_excludes_the_submitting_devices_own_rows(session: Session) -> None:
    product = _product(session)
    store = _store(session, _chain(session, "coles", "supermarket"), "coles store")
    reporter = _device(session, "reporter")
    for price in (1000, 1010, 990, 1005, 995):
        _observe(session, store=store, product=product, price_cents=price, device=reporter)
    for name, price in (("a", 400), ("b", 410), ("c", 405), ("d", 395), ("e", 400)):
        _observe(
            session, store=store, product=product, price_cents=price, device=_device(session, name)
        )

    reference = _reference_price(session, product, store, device_id=reporter.id)

    assert reference is not None
    # The reporter's own $10 series must not become the yardstick for its next
    # submission, or five wrong rows would launder the sixth.
    assert reference.samples == 5
    assert reference.median_cents == pytest.approx(400)
    assert reference.is_outlier(1000)


def test_reference_ignores_rows_that_are_not_accepted(session: Session) -> None:
    product = _product(session)
    chain = _chain(session, "iga", "supermarket")
    store = _store(session, chain, "iga store")
    for price in (400, 410, 420, 430):
        _observe(session, store=store, product=product, price_cents=price)
    for price in (5000, 5100, 5200, 5300):
        _observe(session, store=store, product=product, price_cents=price, status="rejected")

    reference = _reference_price(session, product, store, device_id=None)

    assert reference is not None
    assert reference.samples == 4
    assert reference.median_cents == pytest.approx(415)


def test_reference_ignores_prices_outside_the_recency_window(session: Session) -> None:
    product = _product(session)
    chain = _chain(session, "spudshed", "supermarket")
    store = _store(session, chain, "spudshed store")
    _observe(session, store=store, product=product, price_cents=400, hours_ago=24)
    _observe(session, store=store, product=product, price_cents=410, hours_ago=48)
    for index, price in enumerate((400, 410, 420, 430)):
        _observe(session, store=store, product=product, price_cents=price, hours_ago=24 * (200 + index))

    # Only two rows are recent enough to describe today's price.
    assert _reference_price(session, product, store, device_id=None) is None


def test_no_reference_when_population_is_too_thin(session: Session) -> None:
    product = _product(session)
    store = _seed_chain(session, "coles", "supermarket", [400, 410], product)

    assert _reference_price(session, product, store, device_id=None) is None


def test_fixed_price_chain_tolerates_promos_but_flags_errors(session: Session) -> None:
    product = _product(session)
    # MAD is exactly 0 here: every store charges the same 400c, which is the
    # common case for chain-wide supermarket pricing.
    store = _seed_chain(session, "woolworths", "supermarket", [400, 400, 400, 400, 400], product)

    reference = _reference_price(session, product, store, device_id=None)

    assert reference is not None
    assert reference.scale_cents == pytest.approx(48)  # 12% of 400
    assert not reference.is_outlier(450)
    assert not reference.is_outlier(200)  # half price is a real promo
    assert reference.is_outlier(800)  # the same drink in a different pack size
    assert reference.is_outlier(100)


def test_modified_z_boundary() -> None:
    reference = _Reference(median_cents=400.0, scale_cents=48.0, samples=5)

    assert reference.is_outlier(650)
    assert not reference.is_outlier(649)
    assert reference.is_outlier(150)
    assert not reference.is_outlier(151)
