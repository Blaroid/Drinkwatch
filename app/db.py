"""Database engine, session helpers and schema bootstrap."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import (
    CheckConstraint,
    MetaData,
    UniqueConstraint,
    create_engine,
    inspect as sa_inspect,
    text,
)
from sqlalchemy.engine import Inspector
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

log = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a read session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for workers/scripts: commits on success, rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def wait_for_db(attempts: int = 40, delay: float = 1.5) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("select 1"))
            return
        except Exception as exc:  # pragma: no cover - startup path
            last_error = exc
            log.warning("database not ready (%s/%s): %s", attempt, attempts, exc)
            time.sleep(delay)
    raise RuntimeError(f"database unavailable after {attempts} attempts: {last_error}")


def _unique_keys(inspector: Inspector, table_name: str) -> set[tuple[str, ...]]:
    """Column sets covered by a unique constraint or a unique index."""
    keys = {
        tuple(sorted(constraint["column_names"]))
        for constraint in inspector.get_unique_constraints(table_name)
    }
    keys.update(
        tuple(sorted(index["column_names"]))
        for index in inspector.get_indexes(table_name)
        if index.get("unique")
    )
    return keys


def schema_problems(
    inspector: Inspector | None = None, metadata: MetaData | None = None
) -> list[str]:
    """Ways the database schema disagrees with the models.

    Columns alone are not enough: migration 012 replaced price_daily's primary
    key with a unique index, and a restore that lost schema_migrations can leave
    a database that answers every query while missing a key, a unique constraint
    or a check. Compare those too, so the startup check fails with a readable
    list instead of serving ambiguous rows.

    Only model-declared objects are required to exist. Constraints that a SQL
    migration adds but the models do not describe (the alert triggers, for
    example) are deliberately ignored rather than reported as extra.
    """
    from . import models

    inspector = inspector if inspector is not None else sa_inspect(engine)
    metadata = metadata if metadata is not None else models.Base.metadata

    problems: list[str] = []
    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name):
            problems.append(f"{table.name} (table)")
            continue

        present = {column["name"] for column in inspector.get_columns(table.name)}
        problems.extend(
            f"{table.name}.{column.name} (column)"
            for column in table.columns
            if column.name not in present
        )

        expected_pk = {column.name for column in table.primary_key.columns}
        actual_pk = set(inspector.get_pk_constraint(table.name).get("constrained_columns") or ())
        if actual_pk != expected_pk:
            problems.append(
                f"{table.name} primary key {sorted(actual_pk)} != {sorted(expected_pk)}"
            )

        expected_unique = {
            tuple(sorted(column.name for column in constraint.columns))
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        expected_unique.update(
            tuple(sorted(column.name for column in index.columns))
            for index in table.indexes
            if index.unique
        )
        for columns in sorted(expected_unique - _unique_keys(inspector, table.name)):
            problems.append(f"{table.name} unique ({', '.join(columns)}) is missing")

        expected_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        }
        actual_checks = {
            constraint["name"] for constraint in inspector.get_check_constraints(table.name)
        }
        problems.extend(
            f"{table.name}.{name} (check constraint) is missing"
            for name in sorted(expected_checks - actual_checks)
        )

    return problems


def verify_schema() -> None:
    problems = schema_problems()
    if problems:
        raise RuntimeError(
            "database schema does not match the models; run scripts/migrate.py. Problems: "
            + ", ".join(problems[:10])
            + ("" if len(problems) <= 10 else f" (+{len(problems) - 10} more)")
        )


def init_db() -> None:
    """Create tables that do not exist yet.

    The schema is defined by the SQLAlchemy models in app/models.py. For schema
    changes on a live database, add a migration under migrations/ rather than
    dropping tables.
    """
    from . import models  # noqa: F401  (registers metadata)

    models.Base.metadata.create_all(engine)
