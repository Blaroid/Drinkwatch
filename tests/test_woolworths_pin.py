"""The Woolworths scrape must name a store and carry the state it is pricing.

No network and no database: the HTTP client, the catalogue matcher and the price
writer are faked, so only the scraper's own wiring is under test.
"""

from __future__ import annotations

import types

import pytest

from app.scrape import woolworths
from app.scrape.woolworths import search


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Records the payload instead of posting it to Woolworths."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.product: dict | None = None

    def get(self, url: str, **kwargs) -> None:
        return None

    def post(self, url: str, **kwargs) -> _FakeResponse:
        self.payloads.append(kwargs["json"])
        groups = [{"Products": [self.product]}] if self.product else []
        return _FakeResponse({"Products": groups})


def _fake_settings(
    *, store_id: str, settlement: str = "", state: str = "WA"
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        woolworths_store_id=store_id,
        woolworths_settlement=settlement,
        woolworths_state=state,
    )


@pytest.fixture
def wired(monkeypatch):
    """Wire scrape() to fakes and return (fake client, recorded price writes)."""

    client = _FakeClient()
    recorded: list[dict] = []

    class _Http:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return client

        def __exit__(self, *exc_info) -> bool:
            return False

    def _record_price(session, **kwargs) -> bool:
        recorded.append(kwargs)
        return True

    product = types.SimpleNamespace(id=3, size_ml=500, pack_count=1)
    monkeypatch.setattr(woolworths, "HttpClient", _Http)
    monkeypatch.setattr(
        woolworths, "chain_by_code", lambda session, code: types.SimpleNamespace(id=7)
    )
    monkeypatch.setattr(woolworths, "match_product_woolworths", lambda *a, **k: (product, False))
    monkeypatch.setattr(woolworths, "_upsert_listing", lambda *a, **k: types.SimpleNamespace(id=11))
    monkeypatch.setattr(woolworths, "record_price", _record_price)
    client.product = {
        "DisplayName": "Monster Energy 500ml",
        "Price": 4.5,
        "PackageSize": "500ml",
        "Stockcode": "123",
        "IsAvailable": True,
    }
    return client, recorded


def _session() -> types.SimpleNamespace:
    return types.SimpleNamespace(commit=lambda: None, flush=lambda: None, add=lambda row: None)


def test_pinned_scrape_sends_store_and_stamps_state(monkeypatch, wired) -> None:
    client, recorded = wired
    monkeypatch.setattr(woolworths, "settings", _fake_settings(store_id="1234"))

    woolworths.scrape(_session(), keywords=["monster energy"])

    assert client.payloads[0]["StoreId"] == "1234"
    assert "Settlement" not in client.payloads[0]
    assert recorded[0]["state"] == "WA"
    assert recorded[0]["store_id"] is None
    assert recorded[0]["price_scope"] == "chain"


def test_unpinned_scrape_sends_no_store_and_stamps_no_state(monkeypatch, wired) -> None:
    client, recorded = wired
    monkeypatch.setattr(woolworths, "settings", _fake_settings(store_id=""))

    woolworths.scrape(_session(), keywords=["monster energy"])

    assert "StoreId" not in client.payloads[0]
    assert recorded[0]["state"] is None


def test_search_pins_settlement_when_configured() -> None:
    client = _FakeClient()

    search(client, "monster energy", store_id="1234", settlement="Mount Pleasant")

    assert client.payloads[0]["Settlement"] == "Mount Pleasant"


def test_search_sends_no_store_keys_when_unpinned() -> None:
    client = _FakeClient()

    search(client, "monster energy")

    assert "StoreId" not in client.payloads[0]
    assert "Settlement" not in client.payloads[0]


def test_carton_listing_is_read_from_package_size_and_name_together(monkeypatch, wired) -> None:
    """PackageSize can be the carton total, so the name has to be parsed too.

    Woolworths publishes a carton of 24 x 250mL as PackageSize "6000ML"; reading
    that field alone made 6000 mL a can size and produced the phantom
    "v-blue-6000ml" product instead of the 24-pack it is.
    """
    client, recorded = wired
    seen: list[dict] = []

    def _capture(session, **kwargs):
        seen.append(kwargs)
        return types.SimpleNamespace(id=3), False

    client.product = {
        "DisplayName": "V Energy Drink Blue 250mL",
        "Price": 24.0,
        "PackageSize": "6000ML",
        "Stockcode": "456",
        "IsAvailable": True,
    }
    monkeypatch.setattr(woolworths, "settings", _fake_settings(store_id="1234"))
    monkeypatch.setattr(woolworths, "match_product_woolworths", _capture)

    woolworths.scrape(_session(), keywords=["v energy drink"])

    assert (seen[0]["size_ml"], seen[0]["pack_count"]) == (250, 24)
    assert recorded[0]["price_scope"] == "chain"
