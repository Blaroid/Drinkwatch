"""Re-apply moderation decisions to the daily price ledger.

``price_daily`` used to be written once, at insert: a row flagged for review and
approved later never reached it, and a row rejected later stayed in it, still
distorting the 90-day deal median and the alert baseline. The writers follow the
status now (app/daily.py), but the rows decided while they did not need this
one-time pass.

Each day+scope is replayed from the observations: the newest accepted sighting
is what the ledger should hold, and a row that no accepted sighting supports is
withdrawn (it is left alone when it cannot be traced to one of the group's
observations, so hand-curated history is never guessed at).

    python scripts/repair_price_daily.py [--days 90] [--product-id 359] [--apply]

Dry run by default; pass --apply to write.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

sys.path.insert(0, ".")

from app.daily import local_day, sync_daily_price  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import Observation, PriceDaily  # noqa: E402

Group = tuple[date, int, int, int | None]


def _scope(day: date, observation: Observation) -> Group:
    return (day, observation.chain_id, observation.product_id, observation.store_id)


def _ledger_row(session, group: Group) -> PriceDaily | None:
    day, chain_id, product_id, store_id = group
    return session.scalar(
        select(PriceDaily).where(
            PriceDaily.day == day,
            PriceDaily.chain_id == chain_id,
            PriceDaily.product_id == product_id,
            PriceDaily.store_id.is_(None) if store_id is None else PriceDaily.store_id == store_id,
        )
    )


def _matches(row: PriceDaily | None, observation: Observation) -> bool:
    if row is None:
        return False
    return (row.price_cents, row.was_price_cents, row.promo_kind, row.source) == (
        observation.price_cents,
        observation.was_price_cents,
        observation.promo_kind or "none",
        observation.source,
    )


def _describe(group: Group) -> str:
    day, chain_id, product_id, store_id = group
    return f"{day} chain={chain_id} product={product_id} store={store_id or 'chain'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-apply moderation decisions to price_daily")
    parser.add_argument("--days", type=int, default=90, help="how far back to replay")
    parser.add_argument(
        "--product-id", type=int, action="append", dest="product_ids",
        help="restrict to one product (repeatable)",
    )
    parser.add_argument("--apply", action="store_true", help="write the repair (default: dry run)")
    args = parser.parse_args(argv)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    with session_scope() as session:
        stmt = (
            select(Observation)
            .where(Observation.observed_at >= since)
            .order_by(Observation.observed_at, Observation.id)
        )
        if args.product_ids:
            stmt = stmt.where(Observation.product_id.in_(args.product_ids))
        groups: dict[Group, list[Observation]] = {}
        for observation in session.scalars(stmt):
            groups.setdefault(_scope(local_day(observation.observed_at), observation), []).append(
                observation
            )

        changes = 0
        for group, rows in groups.items():
            current = _ledger_row(session, group)
            accepted = [row for row in rows if row.status == "accepted"]
            if accepted:
                wanted = accepted[-1]
                if _matches(current, wanted):
                    continue
                was = "missing" if current is None else f"{current.price_cents}c {current.source}"
                print(
                    f"{_describe(group)}: {was} -> {wanted.price_cents}c {wanted.source} "
                    f"(observation {wanted.id}, {wanted.status})"
                )
                changes += 1
                if args.apply:
                    sync_daily_price(session, wanted)
                continue

            if current is None:
                continue
            # The row outlived the decision that put it there: find the demoted
            # observation it was written for, and only then withdraw it.
            owner = next(
                (
                    row
                    for row in reversed(rows)
                    if row.source == current.source
                    and row.price_cents == current.price_cents
                    and row.status in ("needs_review", "rejected")
                ),
                None,
            )
            if owner is None:
                print(
                    f"{_describe(group)}: {current.price_cents}c {current.source} unmatched, "
                    "left alone"
                )
                continue
            print(
                f"{_describe(group)}: {current.price_cents}c {current.source} -> withdrawn "
                f"(observation {owner.id}, {owner.status})"
            )
            changes += 1
            if args.apply:
                sync_daily_price(session, owner)

        mode = "applied" if args.apply else "dry run"
        print(f"{mode}: {changes} ledger row(s) {'repaired' if args.apply else 'to repair'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
