"""Matching retailer titles that name a brand and size but no flavour.

An in-memory SQLite database is enough: ``match_product()`` reads brands, sizes
and barcodes, and the curated caffeine table it consults is empty here. No
network, no Postgres.
"""

from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import Base, Barcode, CaffeineReference, Product
from app.scrape.normalize import match_product

TABLES = (Product.__table__, Barcode.__table__, CaffeineReference.__table__)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=TABLES)
    with Session(engine, expire_on_commit=False) as db:
        yield db


def _product(
    db: Session,
    slug: str,
    *,
    name: str,
    size_ml: int = 473,
    pack_count: int = 1,
    brand: str = "Red Bull",
    is_active: bool = True,
) -> Product:
    product = Product(
        slug=slug,
        brand=brand,
        name=name,
        size_ml=size_ml,
        pack_count=pack_count,
        is_active=is_active,
    )
    db.add(product)
    db.flush()
    return product


def _count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Product)) or 0


def test_brand_only_title_matches_the_plain_row_it_describes(session: Session) -> None:
    # The audit's evidence case: Woolworths' title named no flavour, so the
    # scraper created a second row per SKU and split the chains between them.
    existing = _product(session, "red-bull-473ml", name="Original")

    product, created = match_product(
        session, name="Red Bull Energy Drink 473mL", size_ml=473
    )

    assert (product, created) == (existing, False)
    assert _count(session) == 1


def test_brand_only_title_picks_the_only_plain_candidate(session: Session) -> None:
    plain = _product(session, "red-bull-250ml", name="Original", size_ml=250)
    _product(session, "red-bull-sugarfree-250ml", name="Sugarfree", size_ml=250)

    product, created = match_product(session, name="Red Bull 250mL", size_ml=250)

    assert (product, created) == (plain, False)


def test_brand_only_title_creates_the_plain_row_when_only_a_flavour_exists(
    session: Session,
) -> None:
    # A lonely flavoured row is not evidence that "Red Bull 250mL" means it: the
    # title describes the base can, which the catalogue does not have yet.
    _product(session, "red-bull-zero-250ml", name="Zero", size_ml=250)

    product, created = match_product(session, name="Red Bull 250mL", size_ml=250)

    assert created is True
    assert product is not None
    assert product.slug == "red-bull-original-250ml"
    assert product.name == "Original"
    assert _count(session) == 2


def test_brand_only_title_creates_nothing_when_plain_rows_are_ambiguous(
    session: Session,
) -> None:
    _product(session, "monster-green-500ml", name="Original", size_ml=500, brand="Monster")
    _product(session, "monster-500ml", name="Original", size_ml=500, brand="Monster")

    assert match_product(session, name="Monster 500mL", size_ml=500, create=False) == (
        None,
        False,
    )


def test_brand_only_title_ignores_a_deactivated_double(session: Session) -> None:
    # scripts/merge_duplicates.py leaves the merged row inactive; the surviving
    # row is the only candidate the scraper may attach new prices to.
    survivor = _product(session, "red-bull-473ml", name="Original")
    _product(session, "red-bull-original-473ml", name="Original", is_active=False)

    product, created = match_product(
        session, name="Red Bull Energy Drink 473mL", size_ml=473
    )

    assert (product, created) == (survivor, False)


def test_brand_only_title_reactivates_the_plain_row_left_by_a_merge(
    session: Session,
) -> None:
    # The create path lands on this slug, and it is the same can, so its row
    # comes back instead of listings being attached to a hidden product.
    _product(session, "red-bull-zero-250ml", name="Zero", size_ml=250)
    merged = _product(
        session, "red-bull-original-250ml", name="Original", size_ml=250, is_active=False
    )

    product, created = match_product(session, name="Red Bull 250mL", size_ml=250)

    assert (product, created) == (merged, False)
    assert merged.is_active is True
    assert _count(session) == 2


def test_flavoured_title_still_needs_a_matching_flavour(session: Session) -> None:
    ultra = _product(session, "monster-ultra-500ml", name="Ultra", size_ml=500, brand="Monster")
    violet = _product(
        session, "monster-ultra-violet-500ml", name="Ultra Violet", size_ml=500, brand="Monster"
    )

    assert match_product(
        session, name="Monster Ultra Violet 500mL", size_ml=500
    ) == (violet, False)
    product, created = match_product(
        session, name="Monster Aussie Lemonade 500mL", size_ml=500
    )
    assert created is True
    assert product is not None
    assert product.slug == "monster-aussie-lemonade-500ml"
    assert product.id != ultra.id
