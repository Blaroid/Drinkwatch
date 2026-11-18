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
    scheduler.start()
    return scheduler
