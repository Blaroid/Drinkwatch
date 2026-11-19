"""Crowd barcode bindings are governable, and Open Food Facts answers expire.

The defects these tests pin down shared one root: a write nobody could take
back. `submit_pending` indexed the caller's code even when the product already
existed, so a device could attach any code (or a misread) to a known can and
`register_barcode`'s ON CONFLICT never overwrote it; there was no admin surface
to repair a binding; and an Open Food Facts 429 was cached as "not found" and
returned as a truthy suggestion, forever, because OFF_TTL was never read.

The evidence codes from the audit are A_NEW_CODE (a code a later submission
bound to an existing product) and the stale negative for a barcode OFF simply
had not imported yet.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.barcode as barcode_api
from app.api import admin, barcode
from app.db import get_session
from app.models import (
    Base,
    Barcode,
    BarcodeCache,
    CaffeineReference,
    Chain,
    Device,
    Listing,
    Observation,
    Product,
    Store,
)
from app.security import require_admin

# Valid GTIN-13 codes (check digit included): the submission flow now insists on
# one unless a chain published the code. The invalid ones below are the audit's
# misreads and the fixture codes the older tests already use.
NEW_CODE = "9301234567897"
OTHER_CODE = "9312345678907"
BOUND_CODE = "9323456789017"
CREATED_CODE = "9344556677882"
UNPUBLISHED_INVALID = "9300675001235"
PUBLISHED_INVALID = "9300675099984"

# observations.id is a BigInteger primary key, which SQLite will not fill in for
# us; tests always run in one process, so a module counter keeps ids unique.
_OBSERVATION_IDS = itertools.count(1)


@event.listens_for(Observation, "before_insert")
def _assign_observation_id(_mapper, _connection, target: Observation) -> None:
    if target.id is None:
        target.id = next(_OBSERVATION_IDS)


TABLES = (
    Chain.__table__,
    Store.__table__,
    Product.__table__,
    Listing.__table__,
    Barcode.__table__,
    Device.__table__,
    BarcodeCache.__table__,
    CaffeineReference.__table__,
    Observation.__table__,
)


@pytest.fixture()
def session() -> Iterator[Session]:
    # StaticPool keeps one connection alive so the in-memory schema survives
    # across the TestClient's worker thread.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[t for t in TABLES if t.name != "barcode_cache"])
    # barcode_cache.payload is JSONB, which the SQLite dialect cannot render. The
    # column only has to hold a JSON document here; the ORM still serialises and
    # parses it through the model's type, so the cache code runs unchanged.
    with engine.begin() as connection:
        connection.execute(
            text(
                "create table barcode_cache ("
                "barcode varchar(20) primary key, "
                "source varchar(40) not null, "
                "payload json not null, "
                "created_at datetime default current_timestamp)"
            )
        )
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def _product(
    session: Session,
    slug: str,
    *,
    brand: str = "Mother",
    name: str | None = None,
) -> Product:
    product = Product(
        slug=slug,
        brand=brand,
        name=name or slug,
        size_ml=500,
        pack_count=1,
        family_slug=slug,
        category="energy",
        source="auto",
        status="verified",
    )
    session.add(product)
    session.flush()
    return product


@pytest.fixture()
def catalogue(session: Session) -> SimpleNamespace:
    chain = Chain(code="woolworths", name="Woolworths")
    session.add(chain)
    session.flush()
    store = Store(chain_id=chain.id, name="Woolworths Perth", is_active=True)
    session.add(store)
    session.flush()
    return SimpleNamespace(
        chain=chain,
        store=store,
        green=_product(session, "mother-green-500ml", name="Green"),
        ultra=_product(session, "mother-ultra-500ml", name="Ultra"),
    )


class _RecordingLimiter:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def check(self, key: str) -> None:
        self.keys.append(key)


@pytest.fixture()
def read_limiter(monkeypatch: pytest.MonkeyPatch) -> _RecordingLimiter:
    limiter = _RecordingLimiter()
    monkeypatch.setattr(barcode_api, "read_limiter", limiter)
    return limiter


@pytest.fixture()
def client(
    session: Session, catalogue: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    device = Device(token_hash="scanner")
    session.add(device)
    session.flush()
    monkeypatch.setattr(
        barcode_api, "current_device", lambda authorization, *, session: device
    )
    monkeypatch.setattr(barcode_api, "write_limiter", SimpleNamespace(check=lambda key: None))
    monkeypatch.setattr(barcode_api, "read_limiter", SimpleNamespace(check=lambda key: None))
    # No test below may reach Open Food Facts unless it stubs the client.
    monkeypatch.setattr(
        barcode_api, "settings", SimpleNamespace(off_lookup_enabled=False, scrape_user_agent="test")
    )

    app = FastAPI()
    app.include_router(barcode.router)
    app.include_router(admin.router)
    app.dependency_overrides[get_session] = lambda: session
    # The token itself is covered by security tests; these tests are about what
    # the admin routes then do.
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def _pending(
    client: TestClient, barcode_value: str, name: str = "Mother Green 500mL", **extra
):
    payload = {"barcode": barcode_value, "name": name, "brand": "Mother", "size_ml": 500}
    payload.update(extra)
    return client.post("/v1/products/pending", json=payload, headers={"Authorization": "Bearer x"})


def _bindings(session: Session, code: str) -> list[Barcode]:
    return list(session.scalars(select(Barcode).where(Barcode.barcode == code)))


# ---- 1. only the submission that created the product indexes its code ------


def test_submission_does_not_bind_a_new_code_to_an_existing_product(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    # The evidence case: "743265549194 | 360 | crowd - bound by a later
    # submission". Nothing about a new code should reach an existing can, or the
    # next scan of that code answers with the wrong product forever.
    response = _pending(client, NEW_CODE)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is False
    assert body["product"]["id"] == catalogue.green.id
    assert session.scalar(select(func.count()).select_from(Barcode)) == 0
    session.refresh(catalogue.green)
    assert catalogue.green.normalised_barcode is None
    assert catalogue.green.barcode is None


def test_submission_does_not_rebind_a_code_that_belongs_to_another_product(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    session.add(Barcode(barcode=BOUND_CODE, product_id=catalogue.green.id, source="scrape"))
    session.flush()

    # The scan resolves to Green, so the submission lands on Green; what must not
    # happen is Ultra (whose name was typed) gaining a code from this request.
    response = _pending(client, BOUND_CODE, name="Mother Ultra 500mL")

    assert response.status_code == 201, response.text
    assert response.json()["product"]["id"] == catalogue.green.id
    assert response.json()["created"] is False
    rows = _bindings(session, BOUND_CODE)
    assert [row.product_id for row in rows] == [catalogue.green.id]
    session.refresh(catalogue.ultra)
    assert catalogue.ultra.normalised_barcode is None
    assert catalogue.ultra.barcode is None


def test_submission_indexes_the_code_that_created_the_product(
    client: TestClient, session: Session
) -> None:
    response = _pending(client, CREATED_CODE, name="Prime Hydration Blue Raspberry 500mL")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    # Every form the catalogue can be scanned with resolves to the new row.
    rows = list(session.scalars(select(Barcode).where(Barcode.product_id == body["product"]["id"])))
    assert {row.barcode for row in rows} == {CREATED_CODE, "0" + CREATED_CODE}
    # Sourced as a crowd binding so /v1/admin/barcodes can tell it from a scrape.
    assert {row.source for row in rows} == {"crowd"}


def test_submission_keeps_recording_the_price_of_an_existing_product(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    # Not indexing the code must not cost the deployed client its price report.
    response = _pending(
        client, NEW_CODE, store_id=catalogue.store.id, price_cents=350
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is False
    assert body["observation_id"] is not None
    observation = session.get(Observation, body["observation_id"])
    assert observation.product_id == catalogue.green.id
    assert observation.price_cents == 350


# ---- 2. a misread is refused before it becomes a permanent binding ---------


def test_submission_refuses_a_code_with_an_invalid_check_digit(
    client: TestClient, session: Session
) -> None:
    response = _pending(client, UNPUBLISHED_INVALID, name="Prime Hydration Orange 500mL")

    assert response.status_code == 422, response.text
    assert "check digit" in response.json()["detail"]
    assert session.scalar(select(func.count()).select_from(Barcode)) == 0
    assert session.scalar(select(Product.id).where(Product.barcode == UNPUBLISHED_INVALID)) is None


def test_submission_accepts_an_invalid_check_digit_a_chain_published(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    # Woolworths' GTIN-14s and the odd internal code are not valid GTINs, and
    # they arrive through a listing, so the checksum must not block them.
    session.add(
        Listing(
            chain_id=catalogue.chain.id,
            external_sku="wow-1",
            barcode=PUBLISHED_INVALID,
            product_id=None,
        )
    )
    session.flush()

    response = _pending(client, PUBLISHED_INVALID, name="Prime Hydration Orange 500mL")

    assert response.status_code == 201, response.text
    assert response.json()["created"] is True
    assert _bindings(session, PUBLISHED_INVALID)


# ---- 3. the index is moderatable -------------------------------------------


def test_admin_lists_barcode_bindings(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    session.add(Barcode(barcode=BOUND_CODE, product_id=catalogue.green.id, source="crowd"))
    session.add(Barcode(barcode=OTHER_CODE, product_id=catalogue.ultra.id, source="scrape"))
    session.flush()

    listed = client.get("/v1/admin/barcodes").json()
    assert {(row["barcode"], row["product_slug"], row["source"]) for row in listed} == {
        (BOUND_CODE, "mother-green-500ml", "crowd"),
        (OTHER_CODE, "mother-ultra-500ml", "scrape"),
    }
    by_code = {row["barcode"]: row for row in listed}
    assert by_code[BOUND_CODE]["product_label"] == "Mother Green"
    assert by_code[BOUND_CODE]["product_status"] == "verified"

    filtered = client.get("/v1/admin/barcodes", params={"product_slug": "mother-ultra-500ml"})
    assert [row["barcode"] for row in filtered.json()] == [OTHER_CODE]
    crowd = client.get("/v1/admin/barcodes", params={"source": "crowd"})
    assert [row["barcode"] for row in crowd.json()] == [BOUND_CODE]
    # Any form of one physical code finds its rows: a moderator holding the
    # 14-digit form still sees the EAN-13 binding.
    by_form = client.get("/v1/admin/barcodes", params={"barcode": "0" + BOUND_CODE})
    assert [row["barcode"] for row in by_form.json()] == [BOUND_CODE]


def test_admin_delete_unbinds_every_form_of_the_code(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    # A 12-digit UPC-A and the 13-digit form both resolve; deleting one row
    # would leave the scan working through the other, or through the product's
    # own barcode column.
    upc = "641572321887"
    catalogue.green.barcode = upc
    catalogue.green.normalised_barcode = "0" + upc
    session.add(Barcode(barcode=upc, product_id=catalogue.green.id, source="crowd"))
    session.add(Barcode(barcode="0" + upc, product_id=catalogue.green.id, source="crowd"))
    session.flush()
    assert client.get(f"/v1/barcode/{upc}").json()["found"] is True

    response = client.delete(f"/v1/admin/barcodes/{upc}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["removed"]) == sorted([upc, "0" + upc])
    assert body["product_slug"] == "mother-green-500ml"
    assert sorted(body["product_columns_cleared"]) == ["barcode", "normalised_barcode"]
    assert session.scalar(select(func.count()).select_from(Barcode)) == 0
    session.refresh(catalogue.green)
    assert catalogue.green.barcode is None
    assert catalogue.green.normalised_barcode is None
    assert client.get(f"/v1/barcode/{upc}").json()["found"] is False


def test_admin_delete_leaves_a_form_bound_to_another_product(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    upc = "641572321887"
    session.add(Barcode(barcode=upc, product_id=catalogue.green.id, source="crowd"))
    session.add(Barcode(barcode="0" + upc, product_id=catalogue.ultra.id, source="crowd"))
    session.flush()

    body = client.delete(f"/v1/admin/barcodes/{upc}").json()

    assert body["removed"] == [upc]
    assert body["other_products"] == {"0" + upc: catalogue.ultra.id}
    assert [row.product_id for row in _bindings(session, "0" + upc)] == [catalogue.ultra.id]


def test_admin_delete_reports_a_product_column_claim(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    # A product that carries the code in its own columns resolves a scan even
    # with no barcode row, so the moderator has to be told it survives the
    # delete instead of being left to think the code is unbound.
    upc = "641572321887"
    catalogue.ultra.normalised_barcode = "0" + upc
    session.add(Barcode(barcode=upc, product_id=catalogue.green.id, source="crowd"))
    session.flush()

    body = client.delete(f"/v1/admin/barcodes/{upc}").json()

    assert body["removed"] == [upc]
    assert body["other_products"] == {"0" + upc: catalogue.ultra.id}
    session.refresh(catalogue.ultra)
    assert catalogue.ultra.normalised_barcode == "0" + upc


def test_admin_map_moves_a_binding_to_the_right_product(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    catalogue.ultra.barcode = BOUND_CODE
    session.add(Barcode(barcode=BOUND_CODE, product_id=catalogue.ultra.id, source="crowd"))
    session.flush()

    response = client.post(
        f"/v1/admin/barcodes/{BOUND_CODE}/map",
        json={"product_slug": "mother-green-500ml"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["moved"] == [BOUND_CODE]
    assert response.json()["created"] == ["0" + BOUND_CODE]
    assert [row.product_id for row in _bindings(session, BOUND_CODE)] == [catalogue.green.id]
    session.refresh(catalogue.ultra)
    assert catalogue.ultra.barcode is None
    session.refresh(catalogue.green)
    assert catalogue.green.normalised_barcode == BOUND_CODE
    assert client.get(f"/v1/barcode/{BOUND_CODE}").json()["product"]["id"] == catalogue.green.id


def test_admin_map_binds_a_code_the_crowd_flow_refused(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    response = client.post(
        f"/v1/admin/barcodes/{PUBLISHED_INVALID}/map",
        json={"product_slug": "mother-ultra-500ml"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["moved"] == []
    assert sorted(body["created"]) == sorted([PUBLISHED_INVALID, "0" + PUBLISHED_INVALID])
    rows = list(session.scalars(select(Barcode).where(Barcode.product_id == catalogue.ultra.id)))
    assert {row.barcode for row in rows} == {PUBLISHED_INVALID, "0" + PUBLISHED_INVALID}
    assert {row.source for row in rows} == {"admin"}
    assert client.get(f"/v1/barcode/{PUBLISHED_INVALID}").json()["found"] is True


def test_admin_map_takes_the_code_from_the_old_product_columns(
    client: TestClient, session: Session, catalogue: SimpleNamespace
) -> None:
    # The old product can hold the code in normalised_barcode with no barcode row
    # at all (backfill indexes the primary column only). Moving the index row
    # alone would leave the scan resolving to the old product through the column.
    upc = "641572321887"
    catalogue.ultra.normalised_barcode = "0" + upc
    session.add(Barcode(barcode=upc, product_id=catalogue.ultra.id, source="crowd"))
    session.flush()

    response = client.post(
        f"/v1/admin/barcodes/{upc}/map",
        json={"product_slug": "mother-green-500ml"},
    )

    assert response.status_code == 200, response.text
    session.refresh(catalogue.ultra)
    assert catalogue.ultra.normalised_barcode is None
    assert catalogue.ultra.barcode is None
    assert client.get(f"/v1/barcode/{upc}").json()["product"]["id"] == catalogue.green.id
    assert client.get(f"/v1/barcode/0{upc}").json()["product"]["id"] == catalogue.green.id


def test_admin_barcode_routes_404_on_unknown_codes_and_products(
    client: TestClient, catalogue: SimpleNamespace
) -> None:
    assert client.delete("/v1/admin/barcodes/9301234567897").status_code == 404
    missing_product = client.post(
        f"/v1/admin/barcodes/{NEW_CODE}/map", json={"product_slug": "nope-500ml"}
    )
    assert missing_product.status_code == 404


# ---- 4. Open Food Facts answers expire, and failures are not cached ---------


class _OffStub:
    """Stands in for httpx.Client so the Open Food Facts call is offline."""

    def __init__(self, replies: list[tuple[int, dict | None]], calls: list[str]) -> None:
        self._replies = replies
        self._calls = calls

    def __call__(self, *args, **kwargs) -> "_OffStub":
        return self

    def __enter__(self) -> "_OffStub":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get(self, url: str, params: dict | None = None) -> SimpleNamespace:
        self._calls.append(url)
        index = min(len(self._calls) - 1, len(self._replies) - 1)
        status_code, payload = self._replies[index]
        return SimpleNamespace(status_code=status_code, json=lambda: payload)


def _off(monkeypatch: pytest.MonkeyPatch, *replies: tuple[int, dict | None]) -> list[str]:
    calls: list[str] = []
    stub = _OffStub(list(replies), calls)
    monkeypatch.setattr(barcode_api, "httpx", SimpleNamespace(Client=stub))
    monkeypatch.setattr(
        barcode_api, "settings", SimpleNamespace(off_lookup_enabled=True, scrape_user_agent="test")
    )
    return calls


def _cached_payload(session: Session, code: str, payload: dict, age: timedelta) -> None:
    session.add(
        BarcodeCache(
            barcode=code,
            source="openfoodfacts",
            payload=payload,
            created_at=datetime.now(timezone.utc) - age,
        )
    )
    session.flush()


def test_a_fresh_cached_answer_serves_the_request_without_the_network(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _off(monkeypatch)
    _cached_payload(
        session,
        CREATED_CODE,
        {"source": "openfoodfacts", "found": True, "name": "Cached Can"},
        age=timedelta(days=1),
    )

    suggestion = client.get(f"/v1/barcode/{CREATED_CODE}").json()["suggestion"]

    assert suggestion["name"] == "Cached Can"
    assert calls == []


def test_a_cached_negative_is_not_offered_as_a_suggestion(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The evidence case: {"found": false} was passed to clients as a truthy
    # suggestion because a dict is truthy whatever it says.
    calls = _off(monkeypatch)
    _cached_payload(
        session,
        CREATED_CODE,
        {"source": "openfoodfacts", "found": False},
        age=timedelta(days=1),
    )

    body = client.get(f"/v1/barcode/{CREATED_CODE}").json()

    assert body["found"] is False
    assert body["suggestion"] is None
    assert calls == []
    assert barcode_api.external_lookup(session, CREATED_CODE) is None


def test_a_negative_that_aged_out_is_looked_up_again(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OFF_TTL was dead code: a "not found" from before OFF imported the product
    # stayed wrong for the life of the database.
    calls = _off(
        monkeypatch, (200, {"product": {"product_name": "Found Later", "brands": "V"}})
    )
    _cached_payload(
        session,
        CREATED_CODE,
        {"source": "openfoodfacts", "found": False},
        age=barcode_api.OFF_TTL + timedelta(days=1),
    )

    suggestion = client.get(f"/v1/barcode/{CREATED_CODE}").json()["suggestion"]

    assert suggestion["name"] == "Found Later"
    assert len(calls) == 1
    row = session.scalar(select(BarcodeCache).where(BarcodeCache.barcode == CREATED_CODE))
    assert row.payload["found"] is True


def test_a_positive_answer_that_aged_out_is_looked_up_again(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OFF_TTL bounds every cached answer, not just a negative: a label figure or
    # a name can change upstream, and a scan must eventually see the new one.
    calls = _off(
        monkeypatch, (200, {"product": {"product_name": "Renamed", "brands": "Mother"}})
    )
    _cached_payload(
        session,
        CREATED_CODE,
        {"source": "openfoodfacts", "found": True, "name": "Old Name"},
        age=barcode_api.OFF_TTL + timedelta(days=1),
    )

    suggestion = client.get(f"/v1/barcode/{CREATED_CODE}").json()["suggestion"]

    assert suggestion["name"] == "Renamed"
    assert len(calls) == 1


def test_a_negative_answer_from_open_food_facts_is_cached_but_not_served(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 200 with no product is a real answer, so it is worth caching; it is still
    # not a suggestion, and the second scan reads the cache instead of the network.
    calls = _off(monkeypatch, (200, {"product": {}}))

    assert client.get(f"/v1/barcode/{CREATED_CODE}").json()["suggestion"] is None
    row = session.scalar(select(BarcodeCache).where(BarcodeCache.barcode == CREATED_CODE))
    assert row is not None and row.payload["found"] is False
    assert client.get(f"/v1/barcode/{CREATED_CODE}").json()["suggestion"] is None
    assert len(calls) == 1


def test_a_failed_lookup_is_not_cached_as_a_negative(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 429 or a 500 says nothing about the barcode, so the next scan must ask
    # again instead of reading a cached "not found".
    calls = _off(monkeypatch, (429, None), (500, None))

    assert client.get(f"/v1/barcode/{CREATED_CODE}").json()["suggestion"] is None
    assert session.scalar(select(func.count()).select_from(BarcodeCache)) == 0
    assert client.get(f"/v1/barcode/{CREATED_CODE}").json()["suggestion"] is None
    assert len(calls) == 2
    assert session.scalar(select(func.count()).select_from(BarcodeCache)) == 0


# ---- 5. the read endpoint is rate limited ----------------------------------


def test_the_barcode_read_is_rate_limited(
    client: TestClient,
    catalogue: SimpleNamespace,
    read_limiter: _RecordingLimiter,
) -> None:
    assert client.get(f"/v1/barcode/{NEW_CODE}").status_code == 200

    assert read_limiter.keys == ["barcode:testclient"]


def test_the_rate_limit_key_is_the_hop_our_proxy_appended(
    client: TestClient,
    catalogue: SimpleNamespace,
    read_limiter: _RecordingLimiter,
) -> None:
    # A scanner can send any X-Forwarded-For prefix it likes; only the address
    # our own proxy appended is real, so a forged prefix must not mint a new key.
    first = {"x-forwarded-for": "1.2.3.4, 203.0.113.9"}
    second = {"x-forwarded-for": "5.6.7.8, 203.0.113.9"}
    assert client.get(f"/v1/barcode/{NEW_CODE}", headers=first).status_code == 200
    assert client.get(f"/v1/barcode/{NEW_CODE}", headers=second).status_code == 200

    assert read_limiter.keys == ["barcode:203.0.113.9", "barcode:203.0.113.9"]


def test_the_barcode_read_returns_429_when_the_limiter_refuses(
    client: TestClient, catalogue: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(key: str) -> None:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit reached")

    monkeypatch.setattr(barcode_api, "read_limiter", SimpleNamespace(check=_refuse))

    response = client.get(f"/v1/barcode/{NEW_CODE}")

    assert response.status_code == 429
