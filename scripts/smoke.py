"""End-to-end smoke test against a running API.

    python scripts/smoke.py                       # uses PUBLIC_BASE_URL / localhost
    python scripts/smoke.py https://cans.hazzardous.online

Checks the read paths the Android app depends on, plus one crowd write round trip.
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx

LAT, LON = -31.9523, 115.8613  # Perth CBD

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' - ' + detail) if detail and not condition else ''}")
    if not condition:
        failures.append(name)


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

    print()
    if failures:
        print(f"{len(failures)} failure(s): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
