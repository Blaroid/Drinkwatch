"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles  # noqa: F401  (kept for future admin UI)

from . import __version__
from .api import admin, alerts, appdownload, barcode, basket, crowd, photos, public
from .config import settings
from .db import init_db, wait_for_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("drinkwatch")


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    init_db()
    log.info("schema ready")

    scheduler = None
    # The scheduler owns the alert and digest timers as well as the scrapers, so
    # only the scraping jobs follow SCRAPE_ENABLED. Stopping scrapes must not
    # stop alerts on crowd prices (audit #34 item 14).
    if settings.scheduler_enabled:
        from .scheduler import start_scheduler

        scheduler = start_scheduler(settings.scrape_enabled)
        log.info(
            "scheduler started (scrape jobs %s)",
            "enabled" if settings.scrape_enabled else "disabled",
        )

    yield

    if scheduler is not None:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="DrinkWatch API",
    version=__version__,
    summary="Energy drink prices near you, from supermarket sites and crowd sightings",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(public.router)
app.include_router(barcode.router)
app.include_router(basket.router)
app.include_router(alerts.router)
app.include_router(photos.router)
app.include_router(crowd.router)
app.include_router(admin.router)
app.include_router(appdownload.router)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")
