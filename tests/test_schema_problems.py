"""schema_problems() must see drift that a column-only check cannot.

The startup check used to compare columns only, so a price_daily table whose
primary key had been replaced by a unique index still looked correct. These
tests build the smallest table that exercises each comparison in an in-memory
SQLite database, where SQLAlchemy's inspector reports primary keys, unique
constraints and check constraints the same way it does for Postgres.
"""

from __future__ import annotations

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
)
from sqlalchemy.engine import Engine

import app.db as db


def _widgets(
    *,
    primary_key: bool = True,
    store_id: bool = True,
    unique: bool = True,
    check: bool = True,
    extra_check: bool = False,
) -> MetaData:
    metadata = MetaData()
    columns = [
        Column("id", Integer, primary_key=primary_key),
        Column("code", String(20), nullable=False),
    ]
    if store_id:
        columns.append(Column("store_id", Integer))
    constraints: list[CheckConstraint | UniqueConstraint] = []
    if unique:
        constraints.append(UniqueConstraint("code", name="uq_widgets_code"))
    if check:
        constraints.append(
            CheckConstraint("store_id is null or store_id > 0", name="ck_widgets_store")
        )
    if extra_check:
        constraints.append(CheckConstraint("code != ''", name="ck_widgets_extra"))
    Table("widgets", metadata, *columns, *constraints)
    return metadata


def _engine_with(metadata: MetaData | None = None) -> Engine:
    engine = create_engine("sqlite://")
    if metadata is not None:
        metadata.create_all(engine)
    return engine


def test_matching_schema_has_no_problems() -> None:
    engine = _engine_with(_widgets())

    assert db.schema_problems(inspector=inspect(engine), metadata=_widgets()) == []


def test_missing_table_is_reported() -> None:
    assert db.schema_problems(inspector=inspect(_engine_with()), metadata=_widgets()) == [
        "widgets (table)"
    ]


def test_missing_column_is_reported() -> None:
    engine = _engine_with(_widgets(store_id=False, check=False))

    problems = db.schema_problems(inspector=inspect(engine), metadata=_widgets())

    assert "widgets.store_id (column)" in problems
    assert "widgets.ck_widgets_store (check constraint) is missing" in problems


def test_primary_key_drift_is_reported() -> None:
    # The production case: price_daily had no primary key at all, but every
    # column was present, so the old column-only check passed.
    engine = _engine_with(_widgets(primary_key=False))

    problems = db.schema_problems(inspector=inspect(engine), metadata=_widgets())

    assert problems == ["widgets primary key [] != ['id']"]


def test_missing_unique_constraint_is_reported() -> None:
    engine = _engine_with(_widgets(unique=False))

    problems = db.schema_problems(inspector=inspect(engine), metadata=_widgets())

    assert problems == ["widgets unique (code) is missing"]


def test_missing_check_constraint_is_reported() -> None:
    engine = _engine_with(_widgets(check=False))

    problems = db.schema_problems(inspector=inspect(engine), metadata=_widgets())

    assert problems == ["widgets.ck_widgets_store (check constraint) is missing"]


def test_extra_database_constraints_are_not_problems() -> None:
    # The alert constraints exist only in migration 009, not in the metadata.
    # Demanding that the database have no extras would fail every boot.
    engine = _engine_with(_widgets(extra_check=True))

    assert db.schema_problems(inspector=inspect(engine), metadata=_widgets()) == []


def test_verify_schema_raises_with_the_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        db, "schema_problems", lambda: ["price_daily primary key ['day', 'chain_id'] != []"]
    )

    with pytest.raises(RuntimeError, match="price_daily primary key"):
        db.verify_schema()
