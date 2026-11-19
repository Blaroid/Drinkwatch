"""Database engine, session helpers and schema bootstrap."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
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


def missing_columns() -> list[str]:
    """Columns the models expect that the database does not have yet.

    `create_all` only creates missing tables, never missing columns, so a model
    change without a migration is invisible until a query selects the column and
    the API 500s. This turns that into a startup failure with a readable list.
    """
    from sqlalchemy import inspect

    from . import models

    inspector = inspect(engine)
    missing: list[str] = []
    for table in models.Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            missing.append(f"{table.name} (table)")
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        missing.extend(
            f"{table.name}.{column.name}" for column in table.columns if column.name not in present
        )
    return missing


def verify_schema() -> None:
    missing = missing_columns()
    if missing:
        raise RuntimeError(
            "database schema is behind the models; run scripts/migrate.py. Missing: "
            + ", ".join(missing[:10])
            + ("" if len(missing) <= 10 else f" (+{len(missing) - 10} more)")
        )


def init_db() -> None:
    """Create tables that do not exist yet.

    The schema is defined by the SQLAlchemy models in app/models.py. For schema
    changes on a live database, add a migration under migrations/ rather than
    dropping tables.
    """
    from . import models  # noqa: F401  (registers metadata)

    models.Base.metadata.create_all(engine)
