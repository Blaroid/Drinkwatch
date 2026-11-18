"""Seed application against a real (in-memory) database.

SQLite stands in for Postgres here: the two tables involved use no
Postgres-only types, and this keeps the test runnable without a database
service. Timestamps are passed explicitly because SQLite cannot evaluate the
models' ``now()`` server defaults.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.caffeine import apply_reference_caffeine
from app.models import CaffeineReference, Product


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite://")
    Product.__table__.create(engine)
    CaffeineReference.__table__.create(engine)
    now = datetime.now(timezone.utc)
    with Session(engine, autoflush=False, future=True) as db:
        db.add_all(
            [
                CaffeineReference(
                    brand="Monster",
                    flavour="ultra",
                    caffeine_mg_per_100ml=32.0,
                    source="test",
                    created_at=now,
                ),
                CaffeineReference(
                    brand="Monster",
                    flavour="java mean bean",
                    caffeine_mg_per_100ml=42.34,
                    source="test",
                    created_at=now,
                ),
            ]
        )
        db.commit()
        yield db


def _product(name: str, brand: str = "Monster") -> Product:
    now = datetime.now(timezone.utc)
    return Product(
        slug=f"{brand.lower()}-{name.lower().replace(' ', '-')}-500ml",
        brand=brand,
        name=name,
        size_ml=500,
        pack_count=1,
        created_at=now,
        updated_at=now,
    )


def test_seeded_product_is_persisted_with_its_source(session: Session) -> None:
    product = _product("Java Mean Bean")
    assert apply_reference_caffeine(session, product) is True
    assert float(product.caffeine_mg_per_100ml) == 42.34
    assert product.caffeine_source == "seed"

    session.add(product)
    session.commit()
    stored = session.scalar(select(Product).where(Product.slug == product.slug))
    assert stored is not None
    assert float(stored.caffeine_mg_per_100ml) == 42.34
    assert stored.caffeine_source == "seed"


def test_unmatched_product_keeps_the_fallback_marked_as_default(session: Session) -> None:
    product = _product("Mystery Flavour")
    assert apply_reference_caffeine(session, product) is False
    session.add(product)
    session.commit()
    stored = session.scalar(select(Product).where(Product.slug == product.slug))
    assert stored is not None
    # The column default is the FSANZ cap, but provenance says it is a guess.
    assert float(stored.caffeine_mg_per_100ml) == 32.0
    assert stored.caffeine_source == "default"


def test_admin_value_is_not_overwritten(session: Session) -> None:
    product = _product("Java Mean Bean")
    product.caffeine_mg_per_100ml = 12.5
    product.caffeine_source = "admin"
    assert apply_reference_caffeine(session, product) is False
    assert float(product.caffeine_mg_per_100ml) == 12.5
    assert product.caffeine_source == "admin"
