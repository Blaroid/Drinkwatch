"""FuelWatch WA open data: current ULP price per suburb (RSS feed).

FuelWatch publishes a public RSS feed per product/suburb; this is the only
official petrol price source in WA, and it powers the "is the drive worth it?"
part of the route planner.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

import httpx

from ..config import settings

log = logging.getLogger(__name__)

FEED_URL = "https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS"
CACHE_TTL_SECONDS = 1800
_cache: dict[str, tuple[float, dict[str, int]]] = {}

PRICE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*:")
SUBURB_RE = re.compile(r"Address:.*?,\s*([^,]+),\s*Phone", re.I)


def _parse(xml_text: str) -> dict[str, int]:
    """Map suburb (upper case) -> cheapest cents per litre."""
    prices: dict[str, int] = {}
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff"))
    except ET.ParseError as exc:  # pragma: no cover - upstream oddity
        log.warning("fuelwatch feed unparsable: %s", exc)
        return prices
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        price_match = PRICE_RE.match(title)
        suburb_match = SUBURB_RE.search(description)
        if not price_match or not suburb_match:
            continue
        cpl = round(float(price_match.group(1)))
        suburb = suburb_match.group(1).strip().upper()
        if suburb not in prices or cpl < prices[suburb]:
            prices[suburb] = cpl
    return prices


def ulp_prices(product: int = 1) -> dict[str, int]:
    """Cheapest ULP per suburb, cached for 30 minutes."""
    if not settings.fuelwatch_enabled:
        return {}
    key = f"product:{product}"
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        with httpx.Client(timeout=20.0, headers={"User-Agent": settings.scrape_user_agent}) as client:
            response = client.get(FEED_URL, params={"Product": product, "Suburb": "Perth"})
            response.raise_for_status()
            prices = _parse(response.text)
    except Exception as exc:  # pragma: no cover - network path
        log.warning("fuelwatch unavailable: %s", exc)
        return {}
    _cache[key] = (time.monotonic(), prices)
    return prices


def price_for_suburb(suburb: str | None) -> int | None:
    if not suburb:
        return None
    return ulp_prices().get(suburb.strip().upper())
