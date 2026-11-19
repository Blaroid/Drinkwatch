"""Replaying the migration chain must not delete rows or fail on idempotency.

These read the .sql files and, where the statement is portable, execute it
against an in-memory SQLite database seeded with the shape that caused the
production failure. The destructive delete is the evidence case: replaying 002
used to remove every chain-scope sighting (186 rows).
"""

from __future__ import annotations

import pathlib
import re

from sqlalchemy import create_engine

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _migration(name: str) -> str:
    return (ROOT / "migrations" / name).read_text()


def test_chain_scope_cleanup_leaves_real_chain_rows_on_a_replay() -> None:
    statements = re.findall(
        r"delete from observations[^;]*;", _migration("002_chain_scope.sql"), re.IGNORECASE
    )
    assert statements

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table observations ("
            " id integer primary key, store_id integer, chain_id integer, price_scope text)"
        )
        conn.exec_driver_sql(
            "insert into observations (id, store_id, chain_id, price_scope) values"
            " (1, null, 5, 'chain'),"  # the real chain-scope sighting
            " (2, 9, 5, 'store'),"  # a store-scope sighting
            " (3, 9, 5, 'chain')"  # the legacy store-expanded copy
        )
        for statement in statements:
            conn.exec_driver_sql(statement)
        remaining = (
            conn.exec_driver_sql("select id from observations order by id").scalars().all()
        )

    assert remaining == [1, 2]


def test_price_daily_backfill_does_not_name_a_conflict_target() -> None:
    # 012 drops the 3-column primary key that 006 could name as a conflict
    # target, and Postgres rejects the statement at parse time when the target
    # has no matching constraint.
    sql = _migration("006_price_daily.sql").lower()

    assert "on conflict do nothing" in sql
    assert "on conflict (day, chain_id, product_id)" not in sql


def test_alert_constraints_are_dropped_before_they_are_added() -> None:
    # Postgres has no ADD CONSTRAINT IF NOT EXISTS, so a replay only works if
    # each constraint is dropped first.
    sql = _migration("009_alert_radius_and_drop.sql")

    for name in ("ck_alert_trigger", "ck_alert_drop_percent", "ck_alert_location"):
        dropped = sql.find(f"drop constraint if exists {name}")
        added = sql.find(f"add constraint {name}")
        assert dropped >= 0, name
        assert added > dropped, name
