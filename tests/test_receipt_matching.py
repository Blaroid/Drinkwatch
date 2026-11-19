"""Receipt lines have to name the pack, not just the drink.

A 4-pack and a single can share every flavour token, so scoring text alone
always ties and the first database row won. These tests drive the real
``parse_receipt`` handler against an in-memory SQLite catalogue: the API.md
examples are the cases that used to resolve to the wrong pack.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.api.crowd as crowd_api
from app.models import Base, Product
from app.schemas import ReceiptParseIn

TABLES = (Product.__table__,)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=TABLES)
    with Session(engine, expire_on_commit=False) as db:
        yield db


@pytest.fixture(autouse=True)
def _quiet_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The receipt limiter is process-wide; a test must not spend another's budget."""
    monkeypatch.setattr(crowd_api, "read_limiter", SimpleNamespace(check=lambda _key: None))
    monkeypatch.setattr(
        crowd_api,
        "current_device",
        lambda authorization, *, session: SimpleNamespace(id=1),
    )


def _product(
    session: Session,
    slug: str,
    *,
    brand: str,
    name: str,
    size_ml: int | None,
    pack_count: int = 1,
) -> Product:
    product = Product(
        slug=slug,
        brand=brand,
        name=name,
        size_ml=size_ml,
        pack_count=pack_count,
        is_active=True,
    )
    session.add(product)
    return product


@pytest.fixture()
def catalogue(session: Session) -> Session:
    _product(
        session, "monster-ultra-500ml", brand="Monster", name="Ultra", size_ml=500
    )
    _product(
        session,
        "monster-ultra-500ml-x4",
        brand="Monster",
        name="Ultra",
        size_ml=500,
        pack_count=4,
    )
    _product(session, "v-blue-250ml", brand="V", name="Blue", size_ml=250)
    _product(
        session,
        "v-blue-250ml-x4",
        brand="V",
        name="Blue",
        size_ml=250,
        pack_count=4,
    )
    session.flush()
    return session


def _parse(session: Session, text: str) -> list:
    return crowd_api.parse_receipt(
        ReceiptParseIn(text=text), authorization="Bearer test", session=session
    )


def test_a_four_pack_line_matches_the_four_pack(catalogue: Session) -> None:
    # API.md's first example: this used to answer a 250 mL 6-pack.
    [candidate] = _parse(catalogue, "MONSTER ULTRA 4PK 17.50")

    assert candidate.price_cents == 1750
    assert candidate.product_slug == "monster-ultra-500ml-x4"
    assert candidate.label == "Monster Ultra"
    assert candidate.score == 1.0
    assert candidate.alternatives == []


def test_a_single_can_line_matches_the_single_can(catalogue: Session) -> None:
    # API.md's second example: this used to answer v-blue-250ml-x4 because the
    # size and pack were dropped before scoring.
    [candidate] = _parse(catalogue, "V ENERGY BLUE 250ML 3.29")

    assert candidate.price_cents == 329
    assert candidate.product_slug == "v-blue-250ml"
    assert candidate.label == "V Blue"


@pytest.mark.parametrize(
    ("line", "slug"),
    [
        # x4 and "24 pack" are the other forms a docket uses.
        ("V ENERGY BLUE X4 9.99", "v-blue-250ml-x4"),
        ("V ENERGY BLUE 4 X 250ML 9.99", "v-blue-250ml-x4"),
        # A size the catalogue does not have must not fall back to a nearby one.
        ("MONSTER ULTRA 355ML 4.00", None),
        # A pack the catalogue does not have must not fall back to a single.
        ("V ENERGY BLUE 24 PACK 20.00", None),
    ],
)
def test_a_size_or_pack_that_no_row_has_does_not_match(
    catalogue: Session, line: str, slug: str | None
) -> None:
    [candidate] = _parse(catalogue, line)

    assert candidate.product_slug == slug


def test_a_pack_only_line_can_still_tie_across_sizes(catalogue: Session) -> None:
    # "4PK" names no size, so both 4-packs are legitimate; the user is offered
    # both rather than the database order silently choosing a can size.
    _product(
        catalogue,
        "monster-ultra-473ml-x4",
        brand="Monster",
        name="Ultra",
        size_ml=473,
        pack_count=4,
    )
    catalogue.flush()

    [candidate] = _parse(catalogue, "MONSTER ULTRA 4PK 17.50")

    offered = [candidate.product_slug] + [alt.product_slug for alt in candidate.alternatives]
    assert sorted(offered) == ["monster-ultra-473ml-x4", "monster-ultra-500ml-x4"]
    # The primary stays populated so an existing client still preselects a row.
    assert candidate.product_slug == "monster-ultra-473ml-x4"
    assert candidate.alternatives[0].score == candidate.score


def test_equal_rows_are_all_returned_as_alternatives(catalogue: Session) -> None:
    # Two catalogue rows for the same can (a merge candidate): neither is more
    # right than the other, so both have to reach the client.
    _product(
        catalogue,
        "monster-ultra-500ml-coles",
        brand="Monster",
        name="Ultra",
        size_ml=500,
        pack_count=4,
    )
    catalogue.flush()

    [candidate] = _parse(catalogue, "MONSTER ULTRA 4PK 17.50")

    offered = [candidate.product_slug] + [alt.product_slug for alt in candidate.alternatives]
    assert sorted(offered) == [
        "monster-ultra-500ml-coles",
        "monster-ultra-500ml-x4",
    ]
    assert candidate.score == candidate.alternatives[0].score == 1.0


def test_a_line_with_no_size_or_pack_still_offers_both_variants(catalogue: Session) -> None:
    # With nothing to constrain the pack, a bare name is genuinely ambiguous;
    # it must not silently become the single can.
    [candidate] = _parse(catalogue, "MONSTER ULTRA 17.50")

    offered = [candidate.product_slug] + [alt.product_slug for alt in candidate.alternatives]
    assert sorted(offered) == ["monster-ultra-500ml", "monster-ultra-500ml-x4"]


def test_a_rejected_row_is_never_offered(catalogue: Session) -> None:
    rejected = _product(
        catalogue,
        "monster-ultra-500ml-rejected",
        brand="Monster",
        name="Ultra",
        size_ml=500,
        pack_count=4,
    )
    rejected.status = "rejected"
    catalogue.flush()

    [candidate] = _parse(catalogue, "MONSTER ULTRA 4PK 17.50")

    assert candidate.product_slug == "monster-ultra-500ml-x4"
    assert candidate.alternatives == []
