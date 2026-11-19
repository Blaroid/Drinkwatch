"""HTTP caching for /v1/widget: Cache-Control, the ETag and If-None-Match -> 304.

Runs without a database: an explicit product list keeps the route off the
DB-backed helpers, which are stubbed here so the real header handling is
exercised end to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import public
from app.models import Product
from app.schemas import OfferOut

NOW = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)
WIDGET_URL = "/v1/widget?lat=-31.9523&lon=115.8613&products=monster-ultra"


def _product() -> Product:
    return Product(
        id=1,
        slug="monster-ultra",
        brand="Monster",
        name="Ultra",
        size_ml=500,
        pack_count=1,
        category="energy",
        status="approved",
        is_active=True,
    )


def _offer(observed_at: datetime, price_cents: int = 320, distance_km: float = 2.5) -> OfferOut:
    return OfferOut(
        store_id=1,
        store_name="SpudShed Wangara",
        chain_code="spudshed",
        chain_name="SpudShed",
        chain_kind="independent",
        suburb="Wangara",
        distance_km=distance_km,
        price_cents=price_cents,
        price=f"${price_cents / 100:.2f}",
        promo_kind="none",
        source="scrape",
        price_scope="store",
        age_hours=1.0,
        observed_at=observed_at,
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    observed_at: datetime,
    price_cents: int = 320,
    distance_km: float = 2.5,
) -> TestClient:
    app = FastAPI()
    app.include_router(public.router)
    monkeypatch.setattr(public, "get_product_by_slug", lambda session, slug: _product())
    monkeypatch.setattr(public, "nearby_stores", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        public,
        "_offers_for_product",
        lambda *args, **kwargs: [_offer(observed_at, price_cents, distance_km)],
    )
    return TestClient(app)


def test_widget_is_cacheable(monkeypatch: pytest.MonkeyPatch) -> None:
    res = _client(monkeypatch, NOW).get(WIDGET_URL)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "public, max-age=300"
    etag = res.headers["etag"]
    assert etag.startswith('W/"') and etag.endswith('"')
    assert res.json()["items"][0]["slug"] == "monster-ultra"
    # The app can only mark chain prices if the row says what it is; the old
    # payload left it to guess from `source`, which is wrong for scraped shops.
    assert res.json()["items"][0]["price_scope"] == "store"


def test_etag_is_stable_across_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, NOW)
    # generated_at and age_hours move on every request; the validator must not,
    # or the widget would never get a 304.
    assert client.get(WIDGET_URL).headers["etag"] == client.get(WIDGET_URL).headers["etag"]


def test_matching_etag_gets_304_without_a_body(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, NOW)
    etag = client.get(WIDGET_URL).headers["etag"]
    res = client.get(WIDGET_URL, headers={"If-None-Match": etag})
    assert res.status_code == 304
    assert res.content == b""
    assert res.headers["etag"] == etag
    assert res.headers["cache-control"] == "public, max-age=300"


def test_new_observation_invalidates_the_etag(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = _client(monkeypatch, NOW).get(WIDGET_URL).headers["etag"]
    client = _client(monkeypatch, NOW + timedelta(minutes=5))
    assert client.get(WIDGET_URL).headers["etag"] != stale
    assert client.get(WIDGET_URL, headers={"If-None-Match": stale}).status_code == 200


def test_price_change_invalidates_the_etag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same timestamps, edited price: the row fingerprint has to catch it.
    before = _client(monkeypatch, NOW, price_cents=320).get(WIDGET_URL).headers["etag"]
    after = _client(monkeypatch, NOW, price_cents=299).get(WIDGET_URL).headers["etag"]
    assert before != after


def test_row_edit_invalidates_the_etag(monkeypatch: pytest.MonkeyPatch) -> None:
    # A corrected store coordinate changes distance without a new observation.
    before = _client(monkeypatch, NOW, distance_km=2.5).get(WIDGET_URL).headers["etag"]
    after = _client(monkeypatch, NOW, distance_km=3.1).get(WIDGET_URL).headers["etag"]
    assert before != after


def test_etag_is_scoped_to_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, NOW)
    here = client.get(WIDGET_URL).headers["etag"]
    there = client.get(WIDGET_URL.replace("115.8613", "115.9")).headers["etag"]
    assert here != there


def test_if_none_match_accepts_weak_and_listed_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, NOW)
    etag = client.get(WIDGET_URL).headers["etag"]
    strong = etag.removeprefix("W/")
    assert client.get(WIDGET_URL, headers={"If-None-Match": strong}).status_code == 304
    assert client.get(WIDGET_URL, headers={"If-None-Match": f'"other", {etag}'}).status_code == 304
    assert client.get(WIDGET_URL, headers={"If-None-Match": "*"}).status_code == 304
    assert client.get(WIDGET_URL, headers={"If-None-Match": '"other"'}).status_code == 200


def test_widget_with_no_prices_stays_cacheable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, NOW)
    monkeypatch.setattr(public, "_offers_for_product", lambda *args, **kwargs: [])
    res = client.get(WIDGET_URL)
    assert res.status_code == 200
    assert res.json()["items"] == []
    assert res.headers["cache-control"] == "public, max-age=300"
