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
from .api import admin, crowd, public
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
    if settings.scheduler_enabled and settings.scrape_enabled:
        from .scheduler import start_scheduler

        scheduler = start_scheduler()
        log.info("scrape scheduler started")

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
app.include_router(crowd.router)
app.include_router(admin.router)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")
