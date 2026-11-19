"""Unreviewed products must not be pickable from the public catalogue surfaces.

A crowd submission creates the product at ``status='pending'`` until an admin
reviews it. /v1/products filtered ``is_active`` only, so pending rows such as
``monster-smoke-test-500ml`` appeared in the app's search and pickers before
review. The read path now asks for ``verified`` on every surface that lists the
catalogue: search, offline catalogue, deals, and the widget pools (covered in
test_widget_popularity.py and test_widget_suggest.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import public
from app.models import Barcode, Base, Product


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Product.__table__, Barcode.__table__])
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def _product(
    session: Session, product_id: int, slug: str, status: str = "verified"
) -> Product:
    product = Product(
        id=product_id,
        slug=slug,
        brand="Monster",
        name=slug,
        size_ml=500,
        category="energy",
        status=status,
        is_active=True,
    )
    session.add(product)
    return product


def test_public_search_listing_hides_pending_products(session: Session) -> None:
    _product(session, 1, "monster-green-500ml")
    _product(session, 2, "monster-smoke-test-500ml", status="pending")
    _product(session, 3, "monster-dead-500ml", status="rejected")
    session.commit()

    found = public.products(
        q=None, brand=None, category=None, limit=50, offset=0, session=session
    )

    # Search shares this statement, so the q= path cannot surface them either.
    assert [product.slug for product in found] == ["monster-green-500ml"]


def test_offline_catalogue_hides_pending_products(session: Session) -> None:
    verified = _product(session, 1, "monster-green-500ml")
    pending = _product(session, 2, "monster-smoke-test-500ml", status="pending")
    session.add_all(
        [
            Barcode(barcode="9300633099999", product_id=verified.id),
            Barcode(barcode="9300633090000", product_id=pending.id),
        ]
    )
    session.commit()

    payload = public.catalogue(
        Response(), SimpleNamespace(headers={}), session=session
    )

    assert [product.slug for product in payload.products] == ["monster-green-500ml"]
    # The barcode map is what the app uses offline, so a pending can must not
    # resolve from it before review.
    assert payload.barcodes == {"9300633099999": "monster-green-500ml"}


def test_deals_only_price_verified_products(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    verified = _product(session, 1, "monster-green-500ml")
    pending = _product(session, 2, "monster-smoke-test-500ml", status="pending")
    session.commit()
    store = SimpleNamespace(
        id=1,
        chain_id=1,
        name="SpudShed Wangara",
        suburb="Wangara",
        chain=SimpleNamespace(code="spudshed"),
    )
    seen: dict[str, list[int]] = {}

    def _bulk(_session, _stores, product_ids, _since):
        # The price lookup itself is Postgres-only SQL; what matters here is
        # which products the deals board even asks about.
        seen["ids"] = list(product_ids)
        return {}

    monkeypatch.setattr(public, "nearby_stores", lambda *args, **kwargs: [(store, 0.1)])
    monkeypatch.setattr(public, "bulk_latest_prices", _bulk)

    board = public.deals(
        lat=-31.9523,
        lon=115.8613,
        radius_km=15,
        metric="price",
        limit=20,
        max_age_hours=None,
        session=session,
    )

    assert board == []
    assert verified.id in seen["ids"]
    assert pending.id not in seen["ids"]
