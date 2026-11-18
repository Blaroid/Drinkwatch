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


def init_db() -> None:
    """Create tables that do not exist yet.

    The schema is defined by the SQLAlchemy models in app/models.py. For schema
    changes on a live database, add a migration under migrations/ rather than
    dropping tables.
    """
    from . import models  # noqa: F401  (registers metadata)

    models.Base.metadata.create_all(engine)
