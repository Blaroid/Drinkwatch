"""Delete the rows ``scripts/smoke.py`` leaves in a real database.

The smoke test is pointed at a running API, and against production that API
happily accepts everything it posts: a synthetic device (which then shows up on
``/v1/leaderboard``), an accepted crowd price, a 32x32 PNG, a pending
``monster-smoke-test-500ml`` product, an alert and its notification. README
promised this cleanup but the SQL never existed.

The markers are the smoke test's own: ``app_version='smoke'`` (its device),
``display_name`` containing "smoke" (manual probes use the same word), a note
starting ``smoke `` (crowd sightings), the deterministic pending slug, and the
32x32 test image (real photo thumbnails are 320px). Nothing else is touched.

    python scripts/smoke_cleanup.py            # report only, nothing deleted
    python scripts/smoke_cleanup.py --apply

The price_daily row a deleted accepted sighting wrote is withdrawn too, but only
when it matches that sighting exactly and no accepted sighting is left for the
same day and scope; scripts/repair_price_daily.py owns every other ledger repair.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import delete, false, func, or_, select
from sqlalchemy.orm import Session

sys.path.insert(0, ".")

from app.config import settings  # noqa: E402
from app.daily import PerthTZ, local_day  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import (  # noqa: E402
    BarcodeCache,
    CaffeineEntry,
    Device,
    Notification,
    Observation,
    ObservationVote,
    Photo,
    PriceAlert,
    PriceDaily,
    Product,
    Report,
    Watchlist,
)

SMOKE_APP_VERSION = "smoke"
SMOKE_DISPLAY_MARKER = "%smoke%"
SMOKE_NOTE_PREFIX = "smoke %"
# The pending product POST /v1/products/pending derives from the smoke payload
# ("Monster" + "Smoke Test Energy 500mL").
SMOKE_PRODUCT_SLUGS = ("monster-smoke-test-500ml",)
SMOKE_PRODUCT_NAME = "%smoke test%"
# scripts/smoke.py uploads a 32x32 PNG. The photo pipeline stores real uploads at
# up to 1280px and thumbnails at 320px, so 32px is unambiguous test data.
TEST_PHOTO_PIXELS = 32


@dataclass
class Plan:
    devices: list[Device] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    products: list[Product] = field(default_factory=list)
    photos: list[Photo] = field(default_factory=list)
    alerts: list[PriceAlert] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)
    caffeine_entries: list[CaffeineEntry] = field(default_factory=list)
    reports: list[Report] = field(default_factory=list)
    votes: list[ObservationVote] = field(default_factory=list)
    watchlists: list[Watchlist] = field(default_factory=list)
    barcodes: list[BarcodeCache] = field(default_factory=list)
    # (day, chain_id, product_id, store_id, smoke observation) for ledger rows
    # that only the smoke sighting supports.
    daily_rows: list[tuple] = field(default_factory=list)

    @property
    def observation_ids(self) -> list[int]:
        return [row.id for row in self.observations]

    @property
    def device_ids(self) -> list[int]:
        return [row.id for row in self.devices]

    @property
    def product_ids(self) -> list[int]:
        return [row.id for row in self.products]


def _in(column, values: list) -> object:
    """An ``IN`` that stays false (not a syntax error) when the list is empty."""
    return column.in_(values) if values else false()


def _load(session: Session) -> Plan:
    plan = Plan()

    plan.devices = list(
        session.scalars(
            select(Device)
            .where(
                or_(
                    Device.app_version == SMOKE_APP_VERSION,
                    func.lower(Device.display_name).like(SMOKE_DISPLAY_MARKER),
                )
            )
            .order_by(Device.id)
        )
    )
    device_ids = plan.device_ids

    plan.observations = list(
        session.scalars(
            select(Observation)
            .where(
                or_(
                    _in(Observation.device_id, device_ids),
                    func.lower(Observation.note).like(SMOKE_NOTE_PREFIX),
                )
            )
            .order_by(Observation.id)
        )
    )
    observation_ids = plan.observation_ids

    plan.products = list(
        session.scalars(
            select(Product)
            .where(
                or_(
                    Product.slug.in_(SMOKE_PRODUCT_SLUGS),
                    (Product.source == "crowd")
                    & func.lower(Product.name).like(SMOKE_PRODUCT_NAME),
                )
            )
            .order_by(Product.id)
        )
    )

    plan.photos = list(
        session.scalars(
            select(Photo)
            .where(
                or_(
                    _in(Photo.uploaded_by, device_ids),
                    (Photo.width == TEST_PHOTO_PIXELS) & (Photo.height == TEST_PHOTO_PIXELS),
                )
            )
            .order_by(Photo.id)
        )
    )

    plan.alerts = list(
        session.scalars(select(PriceAlert).where(_in(PriceAlert.device_id, device_ids)).order_by(PriceAlert.id))
    )
    plan.notifications = list(
        session.scalars(
            select(Notification)
            .where(_in(Notification.device_id, device_ids))
            .order_by(Notification.id)
        )
    )
    plan.caffeine_entries = list(
        session.scalars(
            select(CaffeineEntry)
            .where(_in(CaffeineEntry.device_id, device_ids))
            .order_by(CaffeineEntry.id)
        )
    )
    plan.reports = list(
        session.scalars(
            select(Report)
            .where(or_(_in(Report.device_id, device_ids), _in(Report.observation_id, observation_ids)))
            .order_by(Report.id)
        )
    )
    plan.votes = list(
        session.scalars(
            select(ObservationVote)
            .where(
                or_(
                    _in(ObservationVote.device_id, device_ids),
                    _in(ObservationVote.observation_id, observation_ids),
                )
            )
            .order_by(ObservationVote.observation_id)
        )
    )
    plan.watchlists = list(
        session.scalars(select(Watchlist).where(_in(Watchlist.device_id, device_ids)))
    )

    smoke_barcodes = sorted(
        {code for product in plan.products for code in (product.barcode, product.normalised_barcode) if code}
    )
    plan.barcodes = list(
        session.scalars(select(BarcodeCache).where(_in(BarcodeCache.barcode, smoke_barcodes)))
    )

    plan.daily_rows = _phantom_daily_rows(session, plan.observations, observation_ids)
    return plan


def _day_bounds(day) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=PerthTZ)
    return start, start + timedelta(days=1)


def _matches_daily(row: PriceDaily, observation: Observation) -> bool:
    return (
        row.price_cents == observation.price_cents
        and row.source == observation.source
        and row.was_price_cents == observation.was_price_cents
        and row.promo_kind == (observation.promo_kind or "none")
    )


def _phantom_daily_rows(
    session: Session, observations: list[Observation], observation_ids: list[int]
) -> list[tuple]:
    """price_daily rows that only to-be-deleted accepted sightings support.

    Deleting the observations would otherwise leave the ledger claiming a price
    nobody reported. A row is only withdrawn when its contents match one of the
    smoke sightings for that day+scope and no accepted sighting is left there,
    so a scrape or a real crowd price is never discarded on a marker alone.
    """
    groups: dict[tuple, list[Observation]] = {}
    for observation in observations:
        if observation.status != "accepted" or observation.observed_at is None:
            continue
        scope = (
            local_day(observation.observed_at),
            observation.chain_id,
            observation.product_id,
            observation.store_id,
        )
        groups.setdefault(scope, []).append(observation)

    phantoms: list[tuple] = []
    for (day, chain_id, product_id, store_id), members in groups.items():
        start, end = _day_bounds(day)
        store_filter = (
            PriceDaily.store_id.is_(None) if store_id is None else PriceDaily.store_id == store_id
        )
        remaining = session.scalar(
            select(func.count())
            .select_from(Observation)
            .where(
                Observation.status == "accepted",
                Observation.observed_at >= start,
                Observation.observed_at < end,
                Observation.id.not_in(observation_ids),
                Observation.chain_id == chain_id,
                Observation.product_id == product_id,
                Observation.store_id.is_(None) if store_id is None else Observation.store_id == store_id,
            )
        )
        if remaining:
            continue

        row = session.scalar(
            select(PriceDaily).where(
                PriceDaily.day == day,
                PriceDaily.chain_id == chain_id,
                PriceDaily.product_id == product_id,
                store_filter,
            )
        )
        if row is None:
            continue
        # Several smoke runs can write the same day+scope; the ledger holds the
        # newest, so match it against every marker row for the group.
        owner = next((member for member in members if _matches_daily(row, member)), None)
        if owner is not None:
            phantoms.append((day, chain_id, product_id, store_id, owner))
    return phantoms


def _thumb_path(filename: str) -> Path:
    path = PurePosixPath(filename)
    return Path(settings.photos_dir) / path.with_name(f"{path.stem}_thumb{path.suffix}")


def _photo_files(photo: Photo) -> list[Path]:
    root = Path(settings.photos_dir)
    return [root / photo.filename, _thumb_path(photo.filename)]


def _report(plan: Plan) -> None:
    def section(title: str, rows: list[str]) -> None:
        print(f"\n{title} ({len(rows)})")
        for row in rows or ["  nothing"]:
            print(f"  {row}" if not row.startswith("  ") else row)

    section(
        "smoke devices",
        [
            f"id={device.id} name={device.display_name!r} app={device.app_version!r}"
            f" reputation={device.reputation}"
            for device in plan.devices
        ],
    )
    section(
        "crowd sightings",
        [
            f"id={observation.id} product={observation.product_id}"
            f" store={observation.store_id or 'chain-wide'} price={observation.price_cents}"
            f" status={observation.status} note={observation.note!r}"
            for observation in plan.observations
        ],
    )
    section(
        "pending products",
        [
            f"id={product.id} slug={product.slug} name={product.name!r}"
            f" barcode={product.normalised_barcode or product.barcode}"
            for product in plan.products
        ],
    )
    section(
        "32x32 test photos",
        [
            f"id={photo.id} token={photo.token} {photo.width}x{photo.height}"
            f" bytes={photo.bytes} uploaded_by={photo.uploaded_by}"
            for photo in plan.photos
        ],
    )
    section(
        "alerts / notifications",
        [
            f"alert id={alert.id} device={alert.device_id} product={alert.product_id}"
            f" max={alert.max_price_cents}"
            for alert in plan.alerts
        ]
        + [
            f"notification id={row.id} device={row.device_id} title={row.title!r}"
            for row in plan.notifications
        ],
    )
    section(
        "caffeine entries / reports / votes / watchlists",
        [
            f"caffeine entry id={row.id} device={row.device_id} mg={row.mg_caffeine}"
            for row in plan.caffeine_entries
        ]
        + [f"report id={row.id} observation={row.observation_id} reason={row.reason!r}" for row in plan.reports]
        + [f"vote observation={row.observation_id} device={row.device_id}" for row in plan.votes]
        + [f"watchlist device={row.device_id}" for row in plan.watchlists],
    )
    section(
        "cached barcode lookups",
        [f"barcode={row.barcode} source={row.source}" for row in plan.barcodes],
    )
    section(
        "price_daily rows only the smoke sighting supports",
        [
            f"{day} chain={chain_id} product={product_id} store={store_id or 'chain'}"
            f" ({observation.price_cents}c {observation.source})"
            for day, chain_id, product_id, store_id, observation in plan.daily_rows
        ],
    )
    files = [path for photo in plan.photos for path in _photo_files(photo) if path.exists()]
    section("photo files on disk", [str(path) for path in files])


def _apply(session: Session, plan: Plan) -> None:
    device_ids = plan.device_ids
    observation_ids = plan.observation_ids

    # Children first: observations and photos use SET NULL on device delete, so
    # deleting the device alone would leave them behind.
    if device_ids:
        session.execute(delete(ObservationVote).where(_in(ObservationVote.device_id, device_ids)))
        session.execute(delete(Report).where(_in(Report.device_id, device_ids)))
        session.execute(delete(Notification).where(_in(Notification.device_id, device_ids)))
        session.execute(delete(PriceAlert).where(_in(PriceAlert.device_id, device_ids)))
        session.execute(delete(CaffeineEntry).where(_in(CaffeineEntry.device_id, device_ids)))
        session.execute(delete(Watchlist).where(_in(Watchlist.device_id, device_ids)))
    if observation_ids:
        session.execute(
            delete(ObservationVote).where(_in(ObservationVote.observation_id, observation_ids))
        )
        session.execute(delete(Report).where(_in(Report.observation_id, observation_ids)))
        session.execute(delete(Observation).where(_in(Observation.id, observation_ids)))
    if plan.photos:
        session.execute(delete(Photo).where(_in(Photo.id, [photo.id for photo in plan.photos])))
    if plan.barcodes:
        session.execute(
            delete(BarcodeCache).where(_in(BarcodeCache.barcode, [row.barcode for row in plan.barcodes]))
        )
    for day, chain_id, product_id, store_id, _observation in plan.daily_rows:
        session.execute(
            delete(PriceDaily).where(
                PriceDaily.day == day,
                PriceDaily.chain_id == chain_id,
                PriceDaily.product_id == product_id,
                PriceDaily.store_id.is_(None) if store_id is None else PriceDaily.store_id == store_id,
            )
        )
    if plan.product_ids:
        session.execute(delete(Product).where(_in(Product.id, plan.product_ids)))
    if device_ids:
        session.execute(delete(Device).where(_in(Device.id, device_ids)))

    photo_files = [path for photo in plan.photos for path in _photo_files(photo)]
    session.flush()
    for path in photo_files:
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge the rows scripts/smoke.py leaves behind")
    parser.add_argument("--apply", action="store_true", help="delete the rows (default: report only)")
    args = parser.parse_args(argv)

    with session_scope() as session:
        plan = _load(session)
        _report(plan)
        if not args.apply:
            print("\ndry run: nothing deleted; re-run with --apply")
            return 0
        _apply(session, plan)
        print(f"\ndeleted {len(plan.devices)} device(s), {len(plan.observations)} sighting(s),"
              f" {len(plan.products)} product(s), {len(plan.photos)} photo(s),"
              f" {len(plan.alerts)} alert(s), {len(plan.notifications)} notification(s),"
              f" {len(plan.daily_rows)} price_daily row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
