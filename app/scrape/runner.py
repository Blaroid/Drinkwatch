"""Run scrapers: python -m app.scrape.runner --chain spudshed"""

from __future__ import annotations

import argparse
import logging
import threading

from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Chain
from . import coles, iga, spudshed, woolworths
from .base import ScrapeStats, begin_run, finish_run

log = logging.getLogger(__name__)

SCRAPERS = {
    "spudshed": spudshed.scrape,
    "woolworths": woolworths.scrape,
    "coles": coles.scrape,
    "iga": iga.scrape,
}

_LOCK = threading.Lock()


def run_chains(codes: list[str], store_ids: list[int] | None = None) -> dict[str, dict]:
    """Run one or more chain scrapers. Non-blocking: returns a 'skipped' marker if busy."""
    if not settings.scrape_enabled:
        return {"skipped": {"reason": "SCRAPE_ENABLED is false"}}
    if not _LOCK.acquire(blocking=False):
        log.warning("scrape already running; skipping %s", codes)
        return {"skipped": {"reason": "another scrape is already running"}}

    results: dict[str, dict] = {}
    try:
        for code in codes:
            scraper = SCRAPERS.get(code)
            if scraper is None:
                results[code] = {"error": f"no scraper registered for '{code}'"}
                continue
            with session_scope() as session:
                run = begin_run(session, code)
                stats = ScrapeStats(chain=code)
                error: str | None = None
                try:
                    stats = scraper(session, store_ids=store_ids, stats=stats)
                    if stats.errors:
                        error = "; ".join(stats.errors[:3])
                except Exception as exc:  # pragma: no cover - network paths
                    log.exception("scrape failed for %s", code)
                    error = f"{type(exc).__name__}: {exc}"
                    session.rollback()
                finish_run(session, run, stats, error=error)
                results[code] = stats.as_dict()
                log.info("%s scrape: %s", code, results[code])
    finally:
        _LOCK.release()
    return results


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Run drinkwatch scrapers")
    parser.add_argument("--chain", action="append", dest="chains", help="chain code (repeatable)")
    parser.add_argument("--all", action="store_true", help="run every chain with scraping enabled")
    parser.add_argument("--store-id", action="append", type=int, dest="store_ids")
    parser.add_argument("--list", action="store_true", help="list registered chains and exit")
    args = parser.parse_args(argv)

    with session_scope() as session:
        enabled = list(session.scalars(select(Chain).where(Chain.scrape_enabled.is_(True))))

    if args.list:
        for chain in enabled:
            print(f"{chain.code:<12} {chain.name:<20} stores_scraped={'yes' if chain.code in SCRAPERS else 'no'}")
        return 0

    codes = args.chains or ([chain.code for chain in enabled] if args.all else [])
    if not codes:
        parser.error("pass --chain <code> or --all")
    results = run_chains(codes, args.store_ids)
    for code, payload in results.items():
        print(code, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
