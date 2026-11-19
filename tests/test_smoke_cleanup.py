"""The smoke test cleans up the rows it writes, keyed on its own markers.

The markers and the deletion live in scripts/smoke.py so a run and its cleanup
cannot drift. The ledger withdrawal goes through app.daily, so an in-memory
SQLite database seeded with only the smoke rows exercises the real path: the
store row leaves and the aggregate chain row that summarised it goes with it.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Iterator

import pytest
from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.orm import Session

from app.models import (
    Base,
    CaffeineEntry,
    Chain,
    Device,
    Listing,
    Notification,
    Observation,
    Photo,
    PriceAlert,
    PriceDaily,
    Product,
    Store,
)
from scripts import smoke

DAY = date(2026, 11, 19)
OBSERVED_AT = datetime(2026, 11, 19, 2, tzinfo=timezone.utc)  # 10:00 in Perth
CHAIN = 1
STORE = 12
OTHER_STORE = 34

TABLES = (
    Chain.__table__,
    Store.__table__,
    Device.__table__,
    Product.__table__,
    Listing.__table__,
    Observation.__table__,
    PriceDaily.__table__,
    Photo.__table__,
    PriceAlert.__table__,
    CaffeineEntry.__table__,
)

# notifications carries a JSONB payload, which SQLite cannot compile; only the
# columns the cleanup touches are needed here.
NOTIFICATIONS_DDL = """
create table notifications (
    id bigint primary key,
    device_id integer not null references devices(id) on delete cascade,
    kind varchar(20) not null default 'alert',
    title varchar(200) not null,
    body text not null,
    payload text,
    read_at timestamp,
    created_at timestamp
)
"""


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=TABLES)
    with engine.begin() as conn:
        conn.exec_driver_sql("pragma foreign_keys=on")
        conn.exec_driver_sql(NOTIFICATIONS_DDL)
        # price_daily keeps a chain row and its store rows for one day, which a
        # three-column primary key cannot; rebuild it as migration 012 keys it.
        conn.exec_driver_sql("create table price_daily_nopk as select * from price_daily")
        conn.exec_driver_sql("drop table price_daily")
        conn.exec_driver_sql("alter table price_daily_nopk rename to price_daily")
        conn.exec_driver_sql(
            "create unique index uq_price_daily_day_chain_product_store"
            " on price_daily (day, chain_id, product_id, store_id)"
        )
    with Session(engine, expire_on_commit=False) as db:
        yield db


@contextmanager
def _scope(session: Session) -> Iterator[Session]:
    yield session


def _location(session: Session) -> None:
    """The chain and the two stores the seeded sightings were made at."""
    session.add(Chain(id=CHAIN, code="spudshed", name="SpudShed"))
    session.flush()
    session.add_all(
        [
            Store(id=STORE, chain_id=CHAIN, name="Store 12"),
            Store(id=OTHER_STORE, chain_id=CHAIN, name="Store 34"),
        ]
    )
    session.flush()


def _device(session: Session, *, name: str, version: str) -> Device:
    device = Device(
        token_hash=f"hash-{name}",
        display_name=name,
        platform="android",
        app_version=version,
    )
    session.add(device)
    session.flush()
    return device


def _product(session: Session, slug: str, name: str, *, status: str = "verified") -> Product:
    product = Product(slug=slug, brand="Monster", name=name, size_ml=500, status=status)
    session.add(product)
    session.flush()
    return product


def _observation(
    session: Session,
    obs_id: int,
    product: Product,
    device: Device | None,
    *,
    note: str | None,
    price_cents: int,
    store_id: int = STORE,
    status: str = "accepted",
) -> Observation:
    observation = Observation(
        id=obs_id,
        store_id=store_id,
        chain_id=CHAIN,
        product_id=product.id,
        price_cents=price_cents,
        price_scope="store",
        source="crowd",
        device_id=device.id if device is not None else None,
        note=note,
        observed_at=OBSERVED_AT,
        status=status,
        dedupe_key=f"crowd:{obs_id}",
    )
    session.add(observation)
    session.flush()
    return observation


def _daily(session: Session, product: Product, *, store_id: int | None, source: str) -> None:
    session.execute(
        insert(PriceDaily).values(
            day=DAY,
            chain_id=CHAIN,
            product_id=product.id,
            store_id=store_id,
            price_cents=399,
            source=source,
        )
    )


def _seed(session: Session) -> dict[str, object]:
    _location(session)
    smoke_device = _device(session, name=smoke.SMOKE_DEVICE_NAME, version=smoke.SMOKE_APP_VERSION)
    # A real device must survive: the marker is the version+name pair, not "has a device".
    real_device = _device(session, name="blaine", version="1.4.2")

    # match_product() stores the flavour only, so the queued row is named
    # "Smoke Test" with the size in its own column; the slug is what the payload
    # derives to.
    smoke_product = _product(
        session, "monster-smoke-test-500ml", smoke.SMOKE_PRODUCT_NAME, status="pending"
    )
    # The run's accepted sighting is on a catalogue can, not on the pending one.
    sighted_product = _product(session, "monster-ultra-500ml", "Ultra")
    real_product = _product(session, "monster-original-500ml", "Original")

    sighting = _observation(
        session,
        1,
        sighted_product,
        smoke_device,
        note=f"{smoke.SMOKE_NOTE_PREFIX}abc123",
        price_cents=399,
    )
    # The pending-product flow writes a sighting with no note; only the device
    # links it to the run, so cleanup cannot rely on the note alone.
    _observation(
        session, 2, smoke_product, smoke_device, note=None, price_cents=399, status="needs_review"
    )
    # A real sighting whose note happens to start with the marker word is not ours.
    real_sighting = _observation(
        session, 3, real_product, real_device, note="smoke flavour is great", price_cents=499,
        store_id=OTHER_STORE,
    )

    _daily(session, sighted_product, store_id=STORE, source="crowd")
    _daily(session, sighted_product, store_id=None, source="aggregate")
    _daily(session, real_product, store_id=OTHER_STORE, source="crowd")
    _daily(session, real_product, store_id=None, source="aggregate")

    session.add_all(
        [
            PriceAlert(device_id=smoke_device.id, product_id=smoke_product.id, max_price_cents=300),
            PriceAlert(device_id=real_device.id, product_id=real_product.id, max_price_cents=450),
            CaffeineEntry(
                id=1, device_id=smoke_device.id, product_id=smoke_product.id, qty=1, mg_caffeine=160
            ),
            CaffeineEntry(
                id=2, device_id=real_device.id, product_id=real_product.id, qty=1, mg_caffeine=160
            ),
            Notification(id=1, device_id=smoke_device.id, title="Price drop", body="smoke"),
            Notification(id=2, device_id=real_device.id, title="Price drop", body="real"),
            Photo(
                id=1,
                token="smoke-photo",
                filename="2099/01/smoke-none.jpg",
                sha256="a" * 64,
                uploaded_by=smoke_device.id,
            ),
            Photo(
                id=2,
                token="real-photo",
                filename="2099/01/real-none.jpg",
                sha256="b" * 64,
                uploaded_by=real_device.id,
            ),
        ]
    )
    session.flush()
    return {
        "smoke_device": smoke_device,
        "real_device": real_device,
        "smoke_product": smoke_product,
        "sighted_product": sighted_product,
        "real_product": real_product,
        "sighting": sighting,
        "real_sighting": real_sighting,
    }


def test_cleanup_deletes_the_rows_a_smoke_run_writes(session: Session) -> None:
    seed = _seed(session)

    report = smoke.purge_smoke_rows(session, apply=True)
    session.flush()

    assert report == smoke.SmokeRows(
        devices=1,
        observations=2,
        ledger=1,
        products=1,
        photos=1,
        alerts=1,
        notifications=1,
        caffeine=1,
    )
    assert session.get(Device, seed["smoke_device"].id) is None
    assert session.get(Device, seed["real_device"].id) is not None
    assert session.scalars(select(Observation.id).order_by(Observation.id)).all() == [3]
    assert session.get(Product, seed["smoke_product"].id) is None
    assert session.get(Product, seed["sighted_product"].id) is not None
    assert session.get(Product, seed["real_product"].id) is not None
    assert session.scalars(select(Photo.token)).all() == ["real-photo"]

    # The fake sighting left the ledger, including the aggregate that summarised
    # its store row; the real store row and its aggregate are untouched.
    daily = session.execute(
        select(PriceDaily.product_id, PriceDaily.store_id, PriceDaily.source).order_by(
            PriceDaily.product_id, PriceDaily.store_id
        )
    ).all()
    assert [(product_id, store_id, source) for product_id, store_id, source in daily] == [
        (seed["real_product"].id, None, "aggregate"),
        (seed["real_product"].id, OTHER_STORE, "crowd"),
    ]

    # Everything that cascaded from the smoke device is gone, and the real
    # device's rows are not.
    assert session.scalar(select(func.count()).select_from(PriceAlert)) == 1
    assert session.scalar(select(func.count()).select_from(Notification)) == 1
    assert session.scalar(select(func.count()).select_from(CaffeineEntry)) == 1
    assert session.scalar(
        select(PriceAlert.device_id).where(PriceAlert.device_id == seed["real_device"].id)
    ) == seed["real_device"].id


def test_dry_run_reports_the_same_counts_without_deleting(session: Session) -> None:
    _seed(session)

    report = smoke.purge_smoke_rows(session, apply=False)

    assert report.devices == 1
    assert report.observations == 2
    assert report.ledger == 1
    assert report.products == 1
    assert report.photos == 1
    assert session.scalar(select(func.count()).select_from(Device)) == 2
    assert session.scalar(select(func.count()).select_from(Observation)) == 3
    assert session.scalar(select(func.count()).select_from(Product)) == 3
    assert session.scalar(select(func.count()).select_from(Photo)) == 2
    assert session.scalar(select(func.count()).select_from(PriceDaily)) == 4


def test_ledger_count_is_distinct_rows_not_sightings(session: Session) -> None:
    seed = _seed(session)
    # The day+scope row is last-writer-wins, so a second run's sighting at the
    # same shop shares the row the first run wrote; it is one row to delete.
    _observation(
        session,
        4,
        seed["sighted_product"],
        seed["smoke_device"],
        note=None,
        price_cents=399,
    )

    report = smoke.purge_smoke_rows(session, apply=False)

    assert report.observations == 3
    assert report.ledger == 1


def test_orphaned_smoke_sighting_is_still_recognised(session: Session) -> None:
    _location(session)
    smoke_device = _device(session, name=smoke.SMOKE_DEVICE_NAME, version=smoke.SMOKE_APP_VERSION)
    product = _product(session, "monster-ultra-500ml", "Ultra")
    _observation(session, 1, product, smoke_device, note=None, price_cents=399)
    # A half-done cleanup can delete the device first, which sets device_id NULL
    # on the sighting; the note marker is what still identifies it.
    orphan = _observation(session, 2, product, None, note=f"{smoke.SMOKE_NOTE_PREFIX}deadbeef",
                          price_cents=400)

    report = smoke.purge_smoke_rows(session, apply=False)

    assert report.observations == 2
    assert session.get(Observation, orphan.id) is not None  # dry run still writes nothing


def test_cli_cleanup_is_a_dry_run_by_default(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(session)
    monkeypatch.setattr("app.db.session_scope", lambda: _scope(session))

    assert smoke.main(["--cleanup"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("would delete 1 device(s), 2 observation(s), 1 price_daily row(s)")
    assert "dry run; pass --apply to delete" in out
    assert session.scalar(select(func.count()).select_from(Device)) == 2


def test_cli_cleanup_applies_and_requires_its_flag(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(session)
    monkeypatch.setattr("app.db.session_scope", lambda: _scope(session))

    assert smoke.main(["--cleanup", "--apply"]) == 0

    assert capsys.readouterr().out.startswith("deleted 1 device(s), 2 observation(s)")
    assert session.scalar(select(func.count()).select_from(Device)) == 1


def test_apply_is_only_valid_with_cleanup(capsys: pytest.CaptureFixture[str]) -> None:
    assert smoke.main(["--apply"]) == 2
    assert "--cleanup" in capsys.readouterr().err
