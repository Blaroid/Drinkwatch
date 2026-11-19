"""End-to-end smoke test against a running API.

    python scripts/smoke.py                       # uses PUBLIC_BASE_URL / localhost
    python scripts/smoke.py https://cans.hazzardous.online
    python scripts/smoke.py --cleanup             # report what runs left behind
    python scripts/smoke.py --cleanup --apply     # delete it (needs the database)

Checks the read paths the Android app depends on, plus one crowd write round trip.

Every run writes real rows: a device, crowd sightings, a photo, an alert and a
pending product. They are marked with the constants below so `--cleanup` can
find them again; the checks stay HTTP-only and the cleanup is a dry run unless
--apply is given. `scripts/purge_smoke.py` runs the same cleanup over a backlog
left by earlier runs.
"""

from __future__ import annotations

import io
import os
import random
import sys
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:  # the database stack is imported lazily; see purge_smoke_rows
    from sqlalchemy.orm import Session

LAT, LON = -31.9523, 115.8613  # Perth CBD

# The markers one run writes. A payload change that stops sending one of these
# would leave rows behind that no cleanup can find, so both sides read them here.
SMOKE_DEVICE_NAME = "smoke-test"
SMOKE_APP_VERSION = "smoke"
SMOKE_NOTE_PREFIX = "smoke "
SMOKE_PRODUCT_TITLE = "Smoke Test Energy 500mL"
# match_product() stores the flavour only and parses the size out of the title,
# so the catalogue marker is the name that leaves "Smoke Test", not the title.
SMOKE_PRODUCT_NAME = "Smoke Test"


@dataclass(frozen=True)
class SmokeRows:
    """How many rows of each kind a smoke run (or a backlog of them) left behind."""

    devices: int
    observations: int
    ledger: int
    products: int
    photos: int
    alerts: int
    notifications: int
    caffeine: int

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' - ' + detail) if detail and not condition else ''}")
    if not condition:
        failures.append(name)


def _tiny_png() -> bytes:
    """Minimal PNG so the photo path can be exercised without binary fixtures."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        import struct

        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    width = height = 32
    raw = b"".join(b"\x00" + bytes([180, 60, 40] * width) for _ in range(height))
    header = __import__("struct").pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _photo_files(filename: str) -> list[Path]:
    """The stored copy and its thumbnail, derived the way the photo routes do."""

    from app.api.photos import thumb_filename
    from app.config import settings

    root = Path(settings.photos_dir)
    return [root / filename, root / thumb_filename(filename)]


def purge_smoke_rows(session: "Session", *, apply: bool) -> SmokeRows:
    """Delete the rows the smoke test wrote, keyed on the markers above.

    Left in the live database they win the crowd leaderboard ("smoke-test" at
    ranks 1-3) and the pending "Smoke Test" product sits in the review queue.
    The sightings also wrote ``price_daily`` rows, so each one is withdrawn the
    way a rejected observation is: only a row that still reflects that sighting
    leaves the ledger, and the chain row rebuilds from whatever remains.

    The database imports sit inside the function because the checks themselves
    are HTTP-only, and running them against a remote API should not need the
    backend installed. Returns the counts so a dry run can report them.
    """

    from sqlalchemy import and_, delete, func, or_, select

    from app.daily import local_day, withdraw_daily_price
    from app.models import (
        CaffeineEntry,
        Device,
        Notification,
        Observation,
        Photo,
        PriceAlert,
        PriceDaily,
        Product,
    )

    def count(model, *criteria) -> int:
        return int(session.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)

    def still_reflects(observation: Observation) -> bool:
        """Whether the day's row is this sighting's, mirroring withdraw_daily_price.

        A row a later scrape or crowd report wrote belongs to that write; counting
        or deleting it here would take history away from a real sighting.
        """
        row = session.execute(
            select(PriceDaily.source, PriceDaily.price_cents).where(
                PriceDaily.day == local_day(observation.observed_at),
                PriceDaily.chain_id == observation.chain_id,
                PriceDaily.product_id == observation.product_id,
                PriceDaily.store_id.is_(None)
                if observation.store_id is None
                else PriceDaily.store_id == observation.store_id,
            )
        ).first()
        return row is not None and (row.source, row.price_cents) == (
            observation.source,
            observation.price_cents,
        )

    device_ids = list(
        session.scalars(
            select(Device.id).where(
                Device.platform == "android",
                Device.app_version == SMOKE_APP_VERSION,
                Device.display_name == SMOKE_DEVICE_NAME,
            )
        )
    )
    # observations.device_id is SET NULL on device delete, so the tagged note is
    # the marker that survives a half-done cleanup. It only counts for an orphaned
    # row: a real sighting whose note happens to start with "smoke " is not ours.
    observations = list(
        session.scalars(
            select(Observation).where(
                or_(
                    Observation.device_id.in_(device_ids),
                    and_(
                        Observation.device_id.is_(None),
                        Observation.note.like(f"{SMOKE_NOTE_PREFIX}%"),
                    ),
                )
            )
        )
    )
    products = list(
        session.scalars(
            select(Product).where(
                Product.name == SMOKE_PRODUCT_NAME,
                Product.status == "pending",
            )
        )
    )
    photos = list(session.scalars(select(Photo).where(Photo.uploaded_by.in_(device_ids))))
    by_device = {
        "alerts": count(PriceAlert, PriceAlert.device_id.in_(device_ids)),
        "notifications": count(Notification, Notification.device_id.in_(device_ids)),
        "caffeine": count(CaffeineEntry, CaffeineEntry.device_id.in_(device_ids)),
    }
    # One ledger row backs every sighting of the same day+scope (last writer
    # wins), so the runs that share a shop would otherwise be counted - and
    # promised for deletion - once per sighting. Report the distinct rows.
    touched_ledger = {
        (
            local_day(observation.observed_at),
            observation.chain_id,
            observation.product_id,
            observation.store_id,
        )
        for observation in observations
        if still_reflects(observation)
    }
    report = SmokeRows(
        devices=len(device_ids),
        observations=len(observations),
        ledger=len(touched_ledger),
        products=len(products),
        photos=len(photos),
        **by_device,
    )
    if not apply:
        return report

    for photo in photos:
        for path in _photo_files(photo.filename):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # the row is the leak that matters; say why the file stayed
                print(f"[warn] could not remove {path}: {exc}", file=sys.stderr)
        session.delete(photo)

    session.execute(delete(Notification).where(Notification.device_id.in_(device_ids)))
    session.execute(delete(PriceAlert).where(PriceAlert.device_id.in_(device_ids)))
    session.execute(delete(CaffeineEntry).where(CaffeineEntry.device_id.in_(device_ids)))

    for observation in observations:
        # The status change is what keeps withdraw_daily_price from picking this
        # row back as its own replacement when it flushes.
        observation.status = "rejected"
        withdraw_daily_price(session, observation)
        session.delete(observation)
        session.flush()
        # withdraw_daily_price loads the ledger row as an ORM object, and
        # PriceDaily's ORM identity is (day, chain, product) - the store is not
        # in it because nothing is meant to load these rows (see models). One
        # session walking several stores' rows for a day would be handed the
        # first as if it were each, so drop it before the next sighting.
        for instance in list(session.identity_map.values()):
            if isinstance(instance, PriceDaily):
                session.expunge(instance)

    for product in products:
        session.delete(product)
    session.flush()
    session.execute(delete(Device).where(Device.id.in_(device_ids)))
    return report


def cleanup_smoke_rows(apply: bool) -> int:
    """CLI body for ``--cleanup``: open the database, report, and delete with --apply."""
    sys.path.insert(0, ".")
    from app.db import session_scope

    with session_scope() as session:
        report = purge_smoke_rows(session, apply=apply)
    mode = "deleted" if apply else "would delete"
    print(
        f"{mode} {report.devices} device(s), {report.observations} observation(s), "
        f"{report.ledger} price_daily row(s), {report.products} pending product(s), "
        f"{report.photos} photo(s), {report.alerts} alert(s), "
        f"{report.notifications} notification(s) and {report.caffeine} caffeine entr(y/ies)"
    )
    if not apply:
        print("dry run; pass --apply to delete")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    cleanup = "--cleanup" in argv
    apply = "--apply" in argv
    rest = [arg for arg in argv if arg not in ("--cleanup", "--apply")]
    if apply and not cleanup:
        print("--apply is only meaningful with --cleanup", file=sys.stderr)
        return 2
    if rest and (cleanup or any(arg.startswith("--") for arg in rest)):
        print("usage: smoke.py [BASE_URL] | smoke.py --cleanup [--apply]", file=sys.stderr)
        return 2
    if cleanup:
        return cleanup_smoke_rows(apply)

    base = rest[0] if rest else os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8788")
    base = base.rstrip("/")
    print(f"target: {base}")
    client = httpx.Client(base_url=base, timeout=30.0)

    health = client.get("/healthz")
    check("healthz", health.status_code == 200 and health.json().get("ok") is True)

    meta = client.get("/v1/meta").json()
    check("meta has chains", len(meta["chains"]) >= 4, str(len(meta["chains"])))
    check("stores seeded", meta["store_count"] > 50, str(meta["store_count"]))
    check("products seeded", meta["product_count"] > 20, str(meta["product_count"]))
    check("prices scraped", meta["observation_count"] > 100, str(meta["observation_count"]))

    stores = client.get(
        "/v1/stores/nearby", params={"lat": LAT, "lon": LON, "radius_km": 5}
    ).json()
    check("nearby stores", len(stores) > 0, str(len(stores)))
    check("stores sorted by distance", all(
        stores[i]["distance_km"] <= stores[i + 1]["distance_km"] for i in range(len(stores) - 1)
    ))

    products = client.get("/v1/products", params={"category": "energy", "limit": 50}).json()
    check("energy products", len(products) > 0, str(len(products)))
    check("no junk products", not any(
        p["slug"].startswith(("monster-1-2m", "bsc-", "blackmores")) for p in products
    ))

    widget = client.get("/v1/widget", params={"lat": LAT, "lon": LON, "radius_km": 25}).json()
    check("widget items", len(widget["items"]) > 0, str(len(widget["items"])))
    check("widget prices positive", all(i["price_cents"] > 0 for i in widget["items"]))

    prices = client.get(
        "/v1/prices",
        params={"product": widget["items"][0]["slug"], "lat": LAT, "lon": LON, "radius_km": 25},
    ).json()
    check("offers for product", len(prices["offers"]) > 0, str(len(prices["offers"])))
    check("offers sorted by price", all(
        prices["offers"][i]["price_cents"] <= prices["offers"][i + 1]["price_cents"]
        for i in range(len(prices["offers"]) - 1)
    ))

    device = client.post(
        "/v1/devices",
        json={
            "platform": "android",
            "app_version": SMOKE_APP_VERSION,
            "display_name": SMOKE_DEVICE_NAME,
        },
    )
    check("device registration", device.status_code == 201, str(device.status_code))
    token = device.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    me = client.get("/v1/me", headers=auth)
    check("device auth", me.status_code == 200)

    bad = client.post(
        "/v1/observations",
        headers=auth,
        json={"store_id": stores[0]["id"], "product_slug": "does-not-exist", "price_cents": 300},
    )
    check("rejects unknown product", bad.status_code in (404, 422), str(bad.status_code))

    if widget["items"]:
        slug = widget["items"][0]["slug"]
        store_id = stores[0]["id"]
        payload = {
            "store_id": store_id,
            "product_slug": slug,
            "price_cents": 399,
            "in_stock": True,
            "note": f"{SMOKE_NOTE_PREFIX}{uuid.uuid4().hex[:6]}",
        }
        created = client.post("/v1/observations", headers=auth, json=payload)
        check("crowd observation", created.status_code == 201, created.text[:120])
        if created.status_code == 201:
            repeat = client.post("/v1/observations", headers=auth, json=payload)
            check(
                "duplicate submission is idempotent",
                repeat.status_code == 201 and repeat.json()["id"] == created.json()["id"],
            )

    # ---- feature pack ----
    search = client.get("/v1/stores/search", params={"q": "spud", "lat": LAT, "lon": LON}).json()
    check("store text search", isinstance(search, list) and len(search) > 0, str(search)[:100])

    # The barcode index is intentionally empty (purged 2026-11-19), so this checks
    # the endpoint contract rather than that a particular code resolves.
    known = client.get("/v1/barcode/19342866000496").json()
    check(
        "barcode lookup answers with a product or a hint",
        (known["found"] and known.get("product")) or bool(known.get("pending_hint")),
        str(known)[:120],
    )

    random_barcode = str(random.randint(10**11, 10**12 - 1))
    unknown = client.get(f"/v1/barcode/{random_barcode}").json()
    check("unknown barcode returns a hint", unknown["found"] is False and unknown["pending_hint"], str(unknown)[:120])

    png = _tiny_png()
    photo = client.post(
        "/v1/photos", headers=auth, files={"file": ("smoke.png", io.BytesIO(png), "image/png")}
    )
    check("photo upload", photo.status_code == 201, photo.text[:120])
    if photo.status_code == 201:
        token = photo.json()["token"]
        served = client.get(f"/v1/photos/{token}")
        check("photo served as jpeg", served.status_code == 200 and served.headers["content-type"] == "image/jpeg")

        pending = client.post(
            "/v1/products/pending",
            headers=auth,
            json={
                "barcode": random_barcode,
                "name": SMOKE_PRODUCT_TITLE,
                "brand": "Monster",
                "store_id": stores[0]["id"],
                "price_cents": 399,
                "photo_token": token,
            },
        )
        check("pending product flow", pending.status_code == 201, pending.text[:160])

    basket = client.post(
        "/v1/basket/compare",
        json={"items": [{"slug": widget["items"][0]["slug"], "quantity": 4}], "lat": LAT, "lon": LON},
    )
    check("basket compare", basket.status_code == 200 and basket.json()["stores"], basket.text[:120])

    budget = client.get(
        "/v1/value/max-caffeine",
        params={"budget_cents": 2000, "lat": LAT, "lon": LON, "radius_km": 25, "mode": "multi_store"},
    )
    check("max-caffeine plan", budget.status_code == 200 and budget.json()["total_caffeine_mg"] > 0,
          budget.text[:120])

    route = client.get(
        "/v1/plan/route",
        params={"items": f"{widget['items'][0]['slug']}:2", "lat": LAT, "lon": LON, "radius_km": 25},
    )
    check("route plan", route.status_code == 200 and route.json()["stops"], route.text[:120])

    alert = client.post(
        "/v1/alerts", headers=auth, json={"product_slug": widget["items"][0]["slug"], "max_price_cents": 100}
    )
    check("alert created", alert.status_code == 201, alert.text[:120])
    check("notifications endpoint", client.get("/v1/notifications", headers=auth).status_code == 200)

    logged = client.post(
        "/v1/caffeine", headers=auth, json={"product_slug": widget["items"][0]["slug"], "qty": 1}
    )
    today = client.get("/v1/caffeine/today", headers=auth)
    check("caffeine log", logged.status_code == 201 and today.json()["total_mg"] > 0,
          logged.text[:120])

    receipt = client.post(
        "/v1/receipts/parse", headers=auth, json={"text": "MONSTER ULTRA 4PK 17.50\nBANANAS 4.50"}
    )
    check("receipt parsing", receipt.status_code == 200 and len(receipt.json()) >= 2, receipt.text[:120])

    batch = client.post(
        "/v1/observations/batch",
        headers=auth,
        json={"items": [
            {"store_id": stores[0]["id"], "product_slug": widget["items"][0]["slug"], "price_cents": 411},
            {"store_id": stores[0]["id"], "product_slug": "nope", "price_cents": 100},
        ]},
    )
    check("batch observations", batch.status_code == 200 and batch.json()[0]["status"] == "accepted"
          and batch.json()[1]["status"] == "error", batch.text[:160])

    board = client.get("/v1/leaderboard", params={"period": "week"})
    check("leaderboard", board.status_code == 200 and isinstance(board.json(), list), board.text[:120])

    print()
    if failures:
        print(f"{len(failures)} failure(s): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
