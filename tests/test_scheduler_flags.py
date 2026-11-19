"""SCRAPE_ENABLED must only gate scraping, never the alert timers.

The old lifespan condition was `scheduler_enabled and scrape_enabled`, so an
operator switching scraping off (to stop hitting the retailers, or during an
outage) also silently stopped the 30-minute alert run -- the one job that exists
for crowd-sighted prices, which scraping does not produce (audit #34 item 14).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app import main, scheduler as scheduler_module
from app.config import settings
from app.scheduler import register_jobs

SCRAPE_JOB_IDS = {"spudshed", "woolworths", "coles"}
ALERT_JOB_IDS = {"alerts", "weekly-digest"}


def _registered(scrape_enabled: bool) -> set[str]:
    scheduler = BackgroundScheduler(timezone=scheduler_module.TIMEZONE)
    register_jobs(scheduler, scrape_enabled)
    return {job.id for job in scheduler.get_jobs()}


def test_scraping_and_scheduler_enabled_registers_every_job() -> None:
    assert _registered(scrape_enabled=True) == SCRAPE_JOB_IDS | ALERT_JOB_IDS


def test_scraping_disabled_still_registers_the_alert_jobs() -> None:
    # The evidence case: SCRAPE_ENABLED=false used to mean no jobs at all.
    registered = _registered(scrape_enabled=False)
    assert registered == ALERT_JOB_IDS
    assert not registered & SCRAPE_JOB_IDS


@pytest.mark.parametrize(
    ("scheduler_enabled", "scrape_enabled", "started", "scrape_jobs"),
    [
        (True, True, True, True),
        (True, False, True, False),
        (False, True, False, False),
        (False, False, False, False),
    ],
)
def test_lifespan_starts_the_scheduler_on_scheduler_enabled_alone(
    monkeypatch: pytest.MonkeyPatch,
    scheduler_enabled: bool,
    scrape_enabled: bool,
    started: bool,
    scrape_jobs: bool,
) -> None:
    """The four flag combinations, checked at the place main.py decides."""
    calls: list[bool] = []

    def fake_start(scrape_enabled: bool) -> None:  # noqa: F811 - test double
        calls.append(scrape_enabled)

    # The lifespan touches the database and the real scheduler; only the
    # decision and the flag it hands over are under test here.
    monkeypatch.setattr(main, "wait_for_db", lambda: None)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(
        main,
        "settings",
        replace(settings, scheduler_enabled=scheduler_enabled, scrape_enabled=scrape_enabled),
    )
    monkeypatch.setattr(scheduler_module, "start_scheduler", fake_start)

    async def run_lifespan() -> None:
        async with main.lifespan(main.app):
            pass

    asyncio.run(run_lifespan())

    assert calls == ([scrape_jobs] if started else [])


def test_start_scheduler_hands_the_scrape_flag_to_the_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[BackgroundScheduler, bool]] = []

    def fake_register(scheduler: BackgroundScheduler, scrape_enabled: bool) -> None:
        seen.append((scheduler, scrape_enabled))

    monkeypatch.setattr(scheduler_module, "register_jobs", fake_register)
    monkeypatch.setattr(BackgroundScheduler, "start", lambda self: None)

    scheduler = scheduler_module.start_scheduler(scrape_enabled=False)

    assert seen == [(scheduler, False)]
