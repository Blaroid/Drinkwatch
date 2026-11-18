"""Scraper plumbing: polite HTTP client, idempotent price writes, run bookkeeping."""

from __future__ import annotations

import logging
import time
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Chain, Observation, PriceDaily, Product, ScrapeRun, Store
from .normalize import per_litre_cents

log = logging.getLogger(__name__)
PerthTZ = zoneinfo.ZoneInfo("Australia/Perth")


@dataclass
class ScrapeStats:
    chain: str = ""
    items_seen: int = 0
    observations_written: int = 0
    products_created: int = 0
    listings_seen: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "chain": self.chain,
            "items_seen": self.items_seen,
            "observations_written": self.observations_written,
            "products_created": self.products_created,
            "listings_seen": self.listings_seen,
            "errors": self.errors[:20],
        }


BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class _Throttled:
    """Spaces requests to a host out, whatever HTTP stack is underneath."""

    min_interval = 1.0
    _last_request = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()


class HttpClient(_Throttled):
    """httpx-backed client with an honest bot user agent."""

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        min_interval: float = 1.0,
        browser: bool = False,
    ) -> None:
        headers = {
            "User-Agent": settings.scrape_browser_ua if browser else settings.scrape_user_agent,
            "Accept-Language": "en-AU,en;q=0.9",
        }
        if browser:
            headers.update(BROWSER_HEADERS)
        self._client = httpx.Client(timeout=timeout, follow_redirects=True, headers=headers)
        self.min_interval = min_interval

    def get(self, url: str, **kwargs) -> httpx.Response:
        self._throttle()
        response = self._client.get(url, **kwargs)
        response.raise_for_status()
        return response

    def post(self, url: str, **kwargs) -> httpx.Response:
        self._throttle()
        response = self._client.post(url, **kwargs)
        response.raise_for_status()
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class CffiClient(_Throttled):
    """curl_cffi-backed client that impersonates Chrome's TLS fingerprint.

    Needed for Coles: Akamai fingerprints the TLS handshake, so a plain httpx
    request with perfect browser headers still gets a bot wall after a few hits.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        min_interval: float = 1.5,
        impersonate: str = "chrome",
    ) -> None:
        from curl_cffi import requests as curl_requests

        self._client = curl_requests.Session(impersonate=impersonate, timeout=timeout)
        self._client.headers.update(
            {
                "Accept-Language": "en-AU,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
            }
        )
        self.min_interval = min_interval

    def get(self, url: str, **kwargs):
        self._throttle()
        response = self._client.get(url, allow_redirects=True, **kwargs)
        response.raise_for_status()
        return response

    def post(self, url: str, **kwargs):
        self._throttle()
        response = self._client.post(url, allow_redirects=True, **kwargs)
        response.raise_for_status()
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CffiClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def record_price(
    session: Session,
    *,
    product: Product,
    chain_id: int,
    store_id: int | None = None,
    price_cents: int,
    was_price_cents: int | None = None,
    promo_kind: str = "none",
    promo_qty: int | None = None,
    promo_total_cents: int | None = None,
    in_stock: bool | None = None,
    listing_id: int | None = None,
    price_scope: str = "store",
    observed_at: datetime | None = None,
    dedupe_key: str | None = None,
    source: str = "scrape",
) -> bool:
    """Insert a price sighting; returns False when this exact sighting already exists."""
    if price_cents <= 0:
        return False
    observed_at = observed_at or datetime.now(timezone.utc)
    location = f"store{store_id}" if store_id is not None else f"chain{chain_id}"
    key = dedupe_key or (
        f"{source}:{location}:{product.id}:{observed_at.date().isoformat()}:"
        f"{price_cents}:{promo_kind}:{promo_qty or 0}:{promo_total_cents or 0}"
    )
    stmt = (
        pg_insert(Observation)
        .values(
            store_id=store_id,
            chain_id=chain_id,
            product_id=product.id,
            listing_id=listing_id,
            price_cents=price_cents,
            was_price_cents=was_price_cents,
            unit_price_cents_per_l=per_litre_cents(price_cents, product.size_ml, product.pack_count),
            promo_kind=promo_kind,
            promo_qty=promo_qty,
            promo_total_cents=promo_total_cents,
            in_stock=in_stock,
            source=source,
            price_scope=price_scope,
            observed_at=observed_at,
            status="accepted",
            dedupe_key=key,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Observation.id)
    )
    inserted = session.execute(stmt).scalar()

    daily = (
        pg_insert(PriceDaily)
        .values(
            day=observed_at.astimezone(PerthTZ).date(),
            chain_id=chain_id,
            product_id=product.id,
            price_cents=price_cents,
            was_price_cents=was_price_cents,
            promo_kind=promo_kind,
            source=source,
        )
        .on_conflict_do_update(
            index_elements=["day", "chain_id", "product_id"],
            set_={
                "price_cents": pg_insert(PriceDaily).excluded.price_cents,
                "was_price_cents": pg_insert(PriceDaily).excluded.was_price_cents,
                "promo_kind": pg_insert(PriceDaily).excluded.promo_kind,
                "source": pg_insert(PriceDaily).excluded.source,
            },
        )
    )
    session.execute(daily)
    return inserted is not None


def begin_run(session: Session, chain_code: str, store_id: int | None = None) -> ScrapeRun:
    run = ScrapeRun(chain_code=chain_code, store_id=store_id, status="running")
    session.add(run)
    session.flush()
    return run


def finish_run(
    session: Session,
    run: ScrapeRun,
    stats: ScrapeStats,
    *,
    error: str | None = None,
) -> None:
    run.finished_at = datetime.now(timezone.utc)
    run.items_seen = stats.items_seen
    run.observations_written = stats.observations_written
    run.products_created = stats.products_created
    run.status = "error" if error else "ok"
    run.error = error


def chain_by_code(session: Session, code: str) -> Chain | None:
    return session.scalar(select(Chain).where(Chain.code == code))


def active_stores(session: Session, chain_id: int) -> list[Store]:
    return list(
        session.scalars(
            select(Store).where(Store.chain_id == chain_id, Store.is_active.is_(True)).order_by(Store.id)
        )
    )
