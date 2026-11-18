"""Apply SQL migrations from migrations/ in filename order.

    python scripts/migrate.py [--dry-run]

Applied files are recorded in schema_migrations. Baseline (001) is implied by
app.db.init_db(), which creates any missing tables from the models.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from app.db import engine  # noqa: E402

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def applied(conn) -> set[str]:
    conn.execute(
        text(
            "create table if not exists schema_migrations ("
            " name text primary key, applied_at timestamptz not null default now())"
        )
    )
    return {row[0] for row in conn.execute(text("select name from schema_migrations"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply SQL migrations")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not MIGRATIONS_DIR.is_dir():
        print("no migrations directory")
        return 0

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with engine.begin() as conn:
        done = applied(conn)
        pending = [path for path in files if path.name not in done]
        print(f"{len(files)} migrations, {len(pending)} pending")
        for path in pending:
            print(f"  applying {path.name} ...")
            if args.dry_run:
                continue
            sql = path.read_text()
            conn.exec_driver_sql(sql)
            conn.execute(
                text("insert into schema_migrations (name) values (:name)"), {"name": path.name}
            )
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
