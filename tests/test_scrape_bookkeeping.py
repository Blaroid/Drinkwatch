"""A failed scrape leaves a run row, and one run writes one sighting per local day.

Two regressions from the same bookkeeping rewrite:

* ``begin_run()`` flushed the ``scrape_runs`` row inside the scraper's
  transaction, so the rollback on an exception expunged a row that was never
  committed and ``finish_run()`` then wrote to that dead object. A crash left no
  trace, and the returned summary credited writes the database had discarded.
* the observation dedupe key used the UTC date while ``price_daily`` keys on the
  Perth date, so a run before 08:00 local saw "yesterday" and wrote a second
  sighting for an unchanged price.

The database tests run on in-memory SQLite with only ``scrape_runs`` created;
the dedupe tests read the statement ``record_price`` builds, because the upsert
itself is Postgres-only.
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

import app.scrape.base as base
import app.scrape.runner as runner
from app.models import Observation, ScrapeRun

# scrape_runs.id is a BigInteger primary key, which SQLite does not fill in; the
# tests run in one process, so a module counter keeps ids unique.
_RUN_IDS = itertools.count(1)


@event.listens_for(ScrapeRun, "before_insert")
def _assign_run_id(_mapper, _connection, target: ScrapeRun) -> None:
    if target.id is None:
        target.id = next(_RUN_IDS)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    ScrapeRun.__table__.create(engine)
    with Session(engine, autoflush=False, expire_on_commit=False) as db:
        yield db


def _runs(session: Session) -> list[ScrapeRun]:
    return list(session.scalars(select(ScrapeRun).order_by(ScrapeRun.id)))


def _scope(session: Session):
    """Stand-in for app.db.session_scope over the test's own session."""

    @contextmanager
    def _session_scope() -> Iterator[Session]:
        yield session
        session.commit()

    return _session_scope


def test_finish_run_records_a_row_the_rollback_expunged(session: Session) -> None:
    run = base.begin_run(session, "coles")
    assert run.id is not None

    # What runner does when the scraper raises: the flushed row was never
    # committed, so the rollback takes it with the scraper's writes.
    session.rollback()
    assert run not in session

    base.finish_run(session, run, base.ScrapeStats(chain="coles"), error="ConnectTimeout: down")
    session.commit()

    rows = _runs(session)
    assert len(rows) == 1
    assert (rows[0].chain_code, rows[0].status) == ("coles", "error")
    assert rows[0].error == "ConnectTimeout: down"
    assert rows[0].finished_at is not None
    # The run still knows when it started, even though the row was re-inserted.
    assert rows[0].started_at is not None


def test_finish_run_updates_a_row_the_scraper_already_committed(session: Session) -> None:
    # SpudShed/Woolworths/Coles commit per store or search term, so a later
    # failure leaves the row on disk; finish_run must update it, not insert a
    # second row for the same run.
    run = base.begin_run(session, "spudshed")
    session.commit()

    session.rollback()
    base.finish_run(
        session, run, base.ScrapeStats(chain="spudshed", observations_written=2), error="boom"
    )
    session.commit()

    rows = _runs(session)
    assert len(rows) == 1
    assert (rows[0].status, rows[0].observations_written) == ("error", 2)


def test_run_chains_records_the_failure_and_zeroes_discarded_writes(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    def _boom(db: Session, store_ids=None, stats: base.ScrapeStats | None = None):
        # The scraper got far enough to count work, then the transaction died.
        stats.items_seen = 4
        stats.observations_written = 3
        stats.products_created = 1
        raise RuntimeError("feed down")

    monkeypatch.setattr(runner, "SCRAPERS", {"coles": _boom})
    monkeypatch.setattr(runner, "session_scope", _scope(session))
    monkeypatch.setattr(runner, "settings", SimpleNamespace(scrape_enabled=True))

    result = runner.run_chains(["coles"])["coles"]

    rows = _runs(session)
    assert len(rows) == 1
    assert (rows[0].status, rows[0].error) == ("error", "RuntimeError: feed down")
    # The summary must not claim rows the rollback threw away: before this fix
    # it returned observations_written=3 with an empty scrape_runs table.
    assert rows[0].observations_written == 0
    assert result["observations_written"] == 0
    assert result["products_created"] == 0
    assert result["items_seen"] == 0


def test_run_chains_still_records_a_successful_run(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    def _ok(db: Session, store_ids=None, stats: base.ScrapeStats | None = None):
        # observations_written stays 0 so the alert evaluation (a database
        # query) is not pulled into this offline test.
        stats.items_seen = 2
        return stats

    monkeypatch.setattr(runner, "SCRAPERS", {"coles": _ok})
    monkeypatch.setattr(runner, "session_scope", _scope(session))
    monkeypatch.setattr(runner, "settings", SimpleNamespace(scrape_enabled=True))

    result = runner.run_chains(["coles"])["coles"]

    rows = _runs(session)
    assert [(row.status, row.items_seen) for row in rows] == [("ok", 2)]
    assert result["items_seen"] == 2


def test_dedupe_key_uses_the_perth_day_not_the_utc_one() -> None:
    # 21:00 UTC is 05:00 the next morning in Perth, before the 08:00 boundary
    # that used to split one local day into two UTC dates.
    observed_at = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)

    key = base.observation_dedupe_key(
        source="scrape",
        location="store12",
        product_id=7,
        price_cents=450,
        promo_kind="none",
        promo_qty=None,
        promo_total_cents=None,
        observed_at=observed_at,
    )

    assert key == "scrape:store12:7:2026-11-18:450:none:0:0"


def test_an_unchanged_price_on_either_side_of_08_00_shares_one_key() -> None:
    # 06:30 and 10:30 on the same Perth day: the morning sighting is still the
    # previous UTC day, which is what produced the duplicate.
    observes = (
        datetime(2026, 11, 17, 22, 30, tzinfo=timezone.utc),
        datetime(2026, 11, 18, 2, 30, tzinfo=timezone.utc),
    )
    keys = {
        base.observation_dedupe_key(
            source="scrape",
            location="store12",
            product_id=7,
            price_cents=450,
            promo_kind="none",
            promo_qty=None,
            promo_total_cents=None,
            observed_at=observed_at,
        )
        for observed_at in observes
    }

    assert len(keys) == 1


def test_record_price_writes_the_local_day_into_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class _Session:
        """Captures the upsert record_price builds before it is executed."""

        def execute(self, statement):
            captured["key"] = statement._values[Observation.__table__.c.dedupe_key].value
            return SimpleNamespace(scalar=lambda: 1)

    # The ledger upsert is Postgres-only; its day is covered by app/daily.py.
    monkeypatch.setattr(base, "record_daily_price", lambda *args, **kwargs: None)
    product = SimpleNamespace(id=7, size_ml=500, pack_count=1)

    written = base.record_price(
        _Session(),
        product=product,
        chain_id=3,
        store_id=12,
        price_cents=450,
        observed_at=datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc),
    )

    assert written is True
    assert captured["key"] == "scrape:store12:7:2026-11-18:450:none:0:0"
