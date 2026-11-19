"""Every documented route must resolve the same code forms.

The regression these tests pin down: /v1/products/barcode/{code} and the
observation write compared the scanned string against products.barcode only,
while /v1/barcode/{code} resolved through the barcodes index, normalised_barcode
and listings.barcode. Codes the catalogue already knew therefore 404'd on the
product route and 422'd on the submission route.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.crowd as crowd_api
from app.api import barcode, crowd, public
from app.db import get_session
from app.models import (
    Base,
    Barcode,
    Chain,
    Device,
    Listing,
    Observation,
    ObservationVote,
    Product,
    Store,
)

# The evidence codes from the audit. The first two are only reachable through
# the barcodes index, the third through products.normalised_barcode.
GTIN14 = "04434835497265"
UPC_A = "641572321887"
NORMALISED = "0122909833026"
# Pre-existing paths that must keep working.
EXACT = "9300675001234"
LISTED = "9342866000496"

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
    Observation.__table__,
    ObservationVote.__table__,
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

    # VOTE_COUNTS sums greatest()/least(); SQLite has only max()/min().
    @event.listens_for(engine, "connect")
    def _register_functions(dbapi_connection, _record) -> None:  # pragma: no cover
        dbapi_connection.create_function("greatest", 2, max)
        dbapi_connection.create_function("least", 2, min)

    Base.metadata.create_all(engine, tables=TABLES)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def _product(
    slug: str, barcode: str | None = None, normalised: str | None = None
) -> Product:
    return Product(
        slug=slug,
        brand="Mother",
        name=slug,
        size_ml=500,
        pack_count=1,
        barcode=barcode,
        normalised_barcode=normalised,
        family_slug=slug,
        category="energy",
        source="auto",
        status="verified",
    )


@pytest.fixture()
def catalogue(session: Session) -> SimpleNamespace:
    """One product per resolution path, each behind a code the client scans."""
    chain = Chain(code="woolworths", name="Woolworths")
    session.add(chain)
    session.flush()
    store = Store(chain_id=chain.id, name="Woolworths Perth", is_active=True)
    session.add(store)

    indexed = _product("mother-indexed-500ml")
    indexed_upc = _product("mother-upc-500ml")
    normalised = _product("mother-normalised-500ml", normalised=NORMALISED)
    exact = _product("mother-exact-500ml", barcode=EXACT)
    listed = _product("mother-listing-500ml")
    session.add_all([indexed, indexed_upc, normalised, exact, listed])
    session.flush()

    session.add_all(
        [
            Barcode(barcode=GTIN14, product_id=indexed.id, source="crowd"),
            Barcode(barcode=UPC_A, product_id=indexed_upc.id, source="crowd"),
            Listing(
                chain_id=chain.id,
                external_sku="123",
                barcode=LISTED,
                product_id=listed.id,
            ),
        ]
    )
    session.flush()

    return SimpleNamespace(
        store=store,
        by_code={
            GTIN14: indexed,
            UPC_A: indexed_upc,
            NORMALISED: normalised,
            EXACT: exact,
            LISTED: listed,
        },
    )


@pytest.fixture()
def client(
    session: Session, catalogue: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    # The daily ledger upsert is Postgres-only and not what these tests cover.
    # crowd.py now funnels every write through daily.sync_daily_price().
    monkeypatch.setattr(crowd_api, "sync_daily_price", lambda *args, **kwargs: None)
    monkeypatch.setattr(crowd_api, "write_limiter", SimpleNamespace(check=lambda key: None))
    device = Device(token_hash="scanner")
    session.add(device)
    session.flush()
    monkeypatch.setattr(
        crowd_api, "current_device", lambda authorization, *, session: device
    )

    app = FastAPI()
    app.include_router(public.router)
    app.include_router(barcode.router)
    app.include_router(crowd.router)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.mark.parametrize("code", [GTIN14, UPC_A, NORMALISED, EXACT, LISTED])
def test_barcode_route_resolves_every_indexed_form(
    client: TestClient, catalogue: SimpleNamespace, code: str
) -> None:
    response = client.get(f"/v1/barcode/{code}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] is True
    assert body["product"]["id"] == catalogue.by_code[code].id


@pytest.mark.parametrize("code", [GTIN14, UPC_A, NORMALISED, EXACT, LISTED])
def test_product_route_resolves_every_indexed_form(
    client: TestClient, catalogue: SimpleNamespace, code: str
) -> None:
    response = client.get(f"/v1/products/barcode/{code}")

    assert response.status_code == 200, response.text
    assert response.json()["id"] == catalogue.by_code[code].id


@pytest.mark.parametrize("code", [GTIN14, UPC_A, NORMALISED, EXACT, LISTED])
def test_observation_write_accepts_every_indexed_form(
    client: TestClient, catalogue: SimpleNamespace, code: str
) -> None:
    response = client.post(
        "/v1/observations",
        json={"store_id": catalogue.store.id, "barcode": code, "price_cents": 350},
        headers={"Authorization": "Bearer scanned"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["product_id"] == catalogue.by_code[code].id


def test_an_unknown_code_still_404s_on_the_product_route(client: TestClient) -> None:
    response = client.get("/v1/products/barcode/9300633099999")

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown barcode: 9300633099999"


def test_an_unknown_code_still_422s_on_the_observation_write(
    client: TestClient, catalogue: SimpleNamespace
) -> None:
    response = client.post(
        "/v1/observations",
        json={"store_id": catalogue.store.id, "barcode": "9300633099999", "price_cents": 350},
        headers={"Authorization": "Bearer scanned"},
    )

    assert response.status_code == 422
    assert "known product_slug or barcode" in response.json()["detail"]


def test_a_code_with_no_canonical_form_does_not_borrow_an_unbackfilled_product(
    client: TestClient,
) -> None:
    # This packaging indicator cannot be folded to EAN-13, so normalize_barcode()
    # returns None and `normalised_barcode == None` would compile to IS NULL. The
    # fixture has products whose normalised_barcode was never filled in, and the
    # lookup must not answer with one of them.
    response = client.get("/v1/barcode/29300675001234")

    assert response.status_code == 200
    assert response.json()["found"] is False
    assert client.get("/v1/products/barcode/29300675001234").status_code == 404
