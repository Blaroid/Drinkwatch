"""Publish drink prices to Home Assistant over MQTT with discovery.

Home Assistant creates the entities by itself once these retained discovery
messages exist, so there is no custom component to install:

    export MQTT_HOST=192.168.1.10 MQTT_USER=... MQTT_PASS=...
    export DW_BASE=https://cans.hazzardous.online DW_LAT=-31.9523 DW_LON=115.8613
    export DW_PRODUCTS=monster-ultra-500ml-x4,v-blue-250ml
    python scripts/ha_bridge.py --loop 900

Each drink becomes a sensor with state = cheapest price (AUD), and attributes for
store, distance, promo type and price scope. Loop mode repolls and republishes.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import httpx

try:  # pragma: no cover - optional dependency
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pip install paho-mqtt to use the Home Assistant bridge") from exc

BASE = os.environ.get("DW_BASE", "http://127.0.0.1:8788").rstrip("/")
LAT = os.environ.get("DW_LAT", "-31.9523")
LON = os.environ.get("DW_LON", "115.8613")
RADIUS = os.environ.get("DW_RADIUS_KM", "20")
PRODUCTS = [p.strip() for p in os.environ.get("DW_PRODUCTS", "").split(",") if p.strip()]
PREFIX = os.environ.get("DW_MQTT_PREFIX", "drinkwatch")
HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
PORT = int(os.environ.get("MQTT_PORT", "1883"))
USER = os.environ.get("MQTT_USER")
PASSWORD = os.environ.get("MQTT_PASS")


def fetch() -> list[dict]:
    params = {"lat": LAT, "lon": LON, "radius_km": RADIUS}
    if PRODUCTS:
        params["products"] = ",".join(PRODUCTS)
    with httpx.Client(timeout=30.0) as client:
        response = client.get(f"{BASE}/v1/widget", params=params)
        response.raise_for_status()
        return response.json().get("items", [])


def publish(client: mqtt.Client, items: list[dict]) -> None:
    for item in items:
        device_id = item["slug"]
        state_topic = f"{PREFIX}/{device_id}/state"
        discovery = {
            "name": item["label"],
            "unique_id": f"drinkwatch_{device_id}",
            "state_topic": state_topic,
            "unit_of_measurement": "AUD",
            "device_class": "monetary",
            "state_class": "total",
            "icon": "mdi:bottle-soda-outline",
            "json_attributes_topic": f"{PREFIX}/{device_id}/attributes",
            "availability_topic": f"{PREFIX}/status",
            "device": {
                "identifiers": ["drinkwatch"],
                "name": "DrinkWatch",
                "manufacturer": "drinkwatch",
                "model": "energy drink prices",
            },
        }
        client.publish(
            f"homeassistant/sensor/drinkwatch_{device_id}/config",
            json.dumps(discovery),
            retain=True,
        )
        client.publish(state_topic, round(item["price_cents"] / 100, 2), retain=True)
        client.publish(
            f"{PREFIX}/{device_id}/attributes",
            json.dumps(
                {
                    "store": item["store"],
                    "chain": item["chain"],
                    "suburb": item.get("suburb"),
                    "distance_km": item.get("distance_km"),
                    "promo": item.get("promo"),
                    "source": item.get("source"),
                    "age_hours": item.get("age_hours"),
                    "per_can_aud": (item["per_can_cents"] / 100) if item.get("per_can_cents") else None,
                }
            ),
            retain=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Home Assistant MQTT bridge")
    parser.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="0 = publish once")
    args = parser.parse_args(argv)

    client = mqtt.Client(client_id="drinkwatch-ha")
    if USER:
        client.username_pw_set(USER, PASSWORD or "")
    client.will_set(f"{PREFIX}/status", "offline", retain=True)
    client.connect(HOST, PORT)
    client.loop_start()
    client.publish(f"{PREFIX}/status", "online", retain=True)

    try:
        while True:
            try:
                items = fetch()
            except Exception as exc:  # pragma: no cover - network path
                print(f"fetch failed: {exc}")
                items = []
            if items:
                publish(client, items)
                print(f"published {len(items)} prices")
            if not args.loop:
                break
            time.sleep(args.loop)
    finally:
        client.publish(f"{PREFIX}/status", "offline", retain=True)
        time.sleep(0.3)
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
