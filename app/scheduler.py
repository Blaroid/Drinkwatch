"""Background scrape schedule (runs inside the API container)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .scrape.runner import run_chains

log = logging.getLogger(__name__)

TIMEZONE = "Australia/Perth"


def safe_run(codes: list[str]) -> None:
    try:
        run_chains(codes)
    except Exception:  # pragma: no cover - defensive
        log.exception("scheduled scrape failed for %s", codes)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=TIMEZONE)

    # SpudShed: per-store sites, cheap to poll, prices change weekly.
    scheduler.add_job(
        safe_run,
        IntervalTrigger(hours=6),
        args=[["spudshed"]],
        id="spudshed",
        max_instances=1,
        coalesce=True,
    )
    # Woolworths / Coles: national catalogue, refresh overnight.
    scheduler.add_job(
        safe_run,
        CronTrigger(hour=5, minute=20, timezone=TIMEZONE),
        args=[["woolworths"]],
        id="woolworths",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        safe_run,
        CronTrigger(hour=5, minute=45, timezone=TIMEZONE),
        args=[["coles"]],
        id="coles",
        max_instances=1,
        coalesce=True,
    )
    # Sunday evening: "where should I buy my cans this week".
    scheduler.add_job(
        safe_digest,
        CronTrigger(day_of_week="sun", hour=18, minute=10, timezone=TIMEZONE),
        id="weekly-digest",
        max_instances=1,
        coalesce=True,
    )
    # Alert evaluation also runs after scrapes; this catches crowd-driven changes.
    scheduler.add_job(
        safe_alerts,
        IntervalTrigger(minutes=30),
        id="alerts",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler


def safe_digest() -> None:
    from .db import session_scope
    from .notify import weekly_digest

    try:
        with session_scope() as session:
            weekly_digest(session)
    except Exception:  # pragma: no cover - defensive
        log.exception("weekly digest failed")


def safe_alerts() -> None:
    from .db import session_scope
    from .notify import evaluate_alerts

    try:
        with session_scope() as session:
            evaluate_alerts(session)
    except Exception:  # pragma: no cover - defensive
        log.exception("scheduled alert evaluation failed")
