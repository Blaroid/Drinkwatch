"""End-to-end smoke test against a running API.

    python scripts/smoke.py                       # uses PUBLIC_BASE_URL / localhost
    python scripts/smoke.py https://cans.hazzardous.online

Checks the read paths the Android app depends on, plus one crowd write round trip.
"""

from __future__ import annotations

import io
import os
import random
import sys
import uuid
import zlib

import httpx

LAT, LON = -31.9523, 115.8613  # Perth CBD

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


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    base = argv[0] if argv else os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8788")
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
        json={"platform": "android", "app_version": "smoke", "display_name": "smoke-test"},
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
            "note": f"smoke {uuid.uuid4().hex[:6]}",
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

    known = client.get("/v1/barcode/19342866000496").json()
    check("barcode resolves a GTIN-14", known["found"] and known["product"]["slug"], str(known)[:120])

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
                "name": "Smoke Test Energy 500mL",
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
