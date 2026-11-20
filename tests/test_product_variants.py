"""The add flow offers the other sizes of the flavour the user picked.

Two problems made a wrong row easy to price:

- the catalogue holds one row per brand+flavour+size+pack, so "V Blue" is six
  rows, and a search result gave no way to say "the one in my hand is the
  500mL" without going back to the search;
- nothing stopped a price being written against the nearest name match.

``flavour_key()`` folds the size and pack out of a row and keeps the brand, and
``/v1/products/{slug}/variants`` returns the rows that share it. Only verified,
active rows are offered, and the row the client already holds is left out.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import public
from app.models import Base, Product
from app.scrape.normalize import flavour_key


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Product.__table__])
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db


def _product(
    session: Session,
    product_id: int,
    slug: str,
    *,
    brand: str = "V",
    name: str = "Energy Drink Blue",
    size_ml: int | None = 250,
    pack_count: int = 1,
    status: str = "verified",
    is_active: bool = True,
    variant: str | None = None,
) -> Product:
    product = Product(
        id=product_id,
        slug=slug,
        brand=brand,
        name=name,
        variant=variant,
        size_ml=size_ml,
        pack_count=pack_count,
        category="energy",
        status=status,
        is_active=is_active,
    )
    session.add(product)
    return product


# ---- the key itself -------------------------------------------------------


@pytest.mark.parametrize(
    ("brand", "name", "variant"),
    [
        ("V", "Energy Drink Blue", None),
        ("V", "Blue 250mL", None),
        ("V", "Blue", "250mL x4"),
        ("v", "BLUE 500ML X 12", "carton"),
    ],
)
def test_size_and_pack_do_not_change_the_flavour_key(
    brand: str, name: str, variant: str | None
) -> None:
    assert flavour_key(brand, name, variant) == "blue v"


def test_a_different_flavour_is_a_different_key() -> None:
    assert flavour_key("V", "Energy Drink Blue") != flavour_key("V", "Energy Drink Green")


def test_the_brand_stays_in_the_key() -> None:
    # Both rows are called "Original"; only the brand tells the cans apart.
    assert flavour_key("Mother", "Original 500mL") != flavour_key("Monster", "Original 500mL")


# ---- the endpoint ---------------------------------------------------------


def test_variants_are_the_other_sizes_of_the_same_flavour(session: Session) -> None:
    _product(session, 1, "v-blue-250ml", size_ml=250)
    _product(session, 2, "v-blue-500ml", name="Energy Drink Blue 500mL", size_ml=500)
    _product(session, 3, "v-blue-250ml-x4", pack_count=4)
    _product(session, 4, "v-blue-500ml-x12", name="Energy Drink Blue 500mL", size_ml=500, pack_count=12)
    _product(session, 5, "v-green-250ml", name="Energy Drink Green")
    session.commit()

    found = public.product_variants("v-blue-250ml", session=session)

    assert [row.slug for row in found] == [
        "v-blue-250ml-x4",
        "v-blue-500ml",
        "v-blue-500ml-x12",
    ]


def test_variants_never_include_the_held_row(session: Session) -> None:
    _product(session, 1, "v-blue-250ml")
    session.commit()

    assert public.product_variants("v-blue-250ml", session=session) == []


def test_variants_skip_rows_that_are_not_in_the_catalogue(session: Session) -> None:
    _product(session, 1, "v-blue-250ml")
    _product(session, 2, "v-blue-355ml", size_ml=355, status="pending")
    _product(session, 3, "v-blue-375ml", size_ml=375, status="rejected")
    _product(session, 4, "v-blue-400ml", size_ml=400, is_active=False)
    session.commit()

    assert public.product_variants("v-blue-250ml", session=session) == []


def test_variants_order_unknown_sizes_last(session: Session) -> None:
    _product(session, 1, "v-blue-250ml")
    _product(session, 2, "v-blue-unknown", size_ml=None)
    _product(session, 3, "v-blue-500ml", size_ml=500)
    session.commit()

    slugs = [row.slug for row in public.product_variants("v-blue-250ml", session=session)]

    assert slugs == ["v-blue-500ml", "v-blue-unknown"]


def test_variants_404_for_an_unknown_slug(session: Session) -> None:
    with pytest.raises(HTTPException) as caught:
        public.product_variants("not-a-drink", session=session)
    assert caught.value.status_code == 404
