"""Freshest-wins pricing: a stale crowd row must not shadow a fresh scrape.

Pure unit tests: no database, no network. `_freshest` is the decision that
`latest_prices()` applies to every store, and the fake session below checks
that both candidate queries are issued even when a store already has its own
row — that gating was the original bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.deps import _freshest, latest_prices
from app.models import Observation

NOW = datetime(2026, 11, 18, 5, 20, tzinfo=timezone.utc)
CHAIN_ID = 1
STORE_ID = 15


def _obs(
    *,
    price_cents: int,
    hours_ago: float,
    store_id: int | None = STORE_ID,
    source: str = "crowd",
    scope: str = "store",
    obs_id: int = 1,
) -> Observation:
    return Observation(
        id=obs_id,
        store_id=store_id,
        chain_id=CHAIN_ID,
        product_id=7,
        price_cents=price_cents,
        promo_kind="none",
        source=source,
        price_scope=scope,
        observed_at=NOW - timedelta(hours=hours_ago),
    )


def test_stale_cheap_store_row_loses_to_fresh_chain_scrape() -> None:
    """71h-old $3.15 crowd report vs last night's $3.50 online scrape."""
    stale_crowd = _obs(price_cents=315, hours_ago=71.0, obs_id=10)
    fresh_scrape = _obs(
        price_cents=350,
        hours_ago=1.0,
        store_id=None,
        source="scrape",
        scope="chain",
        obs_id=11,
    )

    chosen = _freshest(stale_crowd, fresh_scrape, STORE_ID)

    assert chosen is not None
    assert chosen.primary.price_cents == 350
    assert chosen.primary.price_scope == "chain"
    # The chain row has store_id NULL in the ledger; the offer must be stamped
    # with the store it is being attributed to.
    assert chosen.primary.store_id == STORE_ID
    assert chosen.primary.observed_at == fresh_scrape.observed_at
    # The loser is kept, not dropped.
    assert chosen.fallback is not None
    assert chosen.fallback.price_cents == 315


def test_fresh_store_sighting_still_beats_older_chain_price() -> None:
    fresh_crowd = _obs(price_cents=315, hours_ago=0.5, obs_id=12)
    older_chain = _obs(
        price_cents=350,
        hours_ago=20.0,
        store_id=None,
        source="scrape",
        scope="chain",
        obs_id=13,
    )

    chosen = _freshest(fresh_crowd, older_chain, STORE_ID)

    assert chosen is not None
    assert chosen.primary.price_cents == 315
    assert chosen.primary.price_scope == "store"
    assert chosen.fallback is not None
    assert chosen.fallback.price_cents == 350


def test_tie_prefers_the_store_scoped_row() -> None:
    """Equal timestamps: the row seen on that shelf wins over the proxy."""
    store_row = _obs(price_cents=315, hours_ago=2.0, obs_id=14)
    chain_row = _obs(
        price_cents=350, hours_ago=2.0, store_id=None, source="scrape", scope="chain", obs_id=15
    )

    chosen = _freshest(store_row, chain_row, STORE_ID)

    assert chosen is not None
    assert chosen.primary.price_scope == "store"


def test_naive_timestamps_do_not_break_the_comparison() -> None:
    store_row = _obs(price_cents=315, hours_ago=71.0, obs_id=16)
    store_row.observed_at = store_row.observed_at.replace(tzinfo=None)
    chain_row = _obs(
        price_cents=350, hours_ago=1.0, store_id=None, source="scrape", scope="chain", obs_id=17
    )

    chosen = _freshest(store_row, chain_row, STORE_ID)

    assert chosen is not None
    assert chosen.primary.price_cents == 350


def test_single_candidate_is_used_without_fallback() -> None:
    only_store = _obs(price_cents=315, hours_ago=71.0, obs_id=18)
    only_chain = _obs(
        price_cents=350, hours_ago=1.0, store_id=None, source="scrape", scope="chain", obs_id=19
    )

    store_choice = _freshest(only_store, None, STORE_ID)
    chain_choice = _freshest(None, only_chain, STORE_ID)

    assert store_choice is not None and store_choice.fallback is None
    assert store_choice.primary.price_cents == 315
    assert chain_choice is not None and chain_choice.fallback is None
    assert chain_choice.primary.price_cents == 350
    assert chain_choice.primary.store_id == STORE_ID


def test_no_candidates_return_nothing() -> None:
    assert _freshest(None, None, STORE_ID) is None


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict]:
        return self._rows


class _FakeSession:
    """Just enough of Session.execute to run latest_prices()' two queries."""

    def __init__(self, store_rows: list[dict], chain_rows: list[dict]) -> None:
        self.store_rows = store_rows
        self.chain_rows = chain_rows
        self.chain_queries: list[dict] = []

    def execute(self, statement, params):  # noqa: ANN001 - duck-typed test double
        # The chain-scope query is the only one that filters on a NULL store_id.
        if "o.store_id is null" in str(statement):
            self.chain_queries.append(params)
            return _FakeResult(self.chain_rows)
        return _FakeResult(self.store_rows)


class _FakeStore:
    def __init__(self, store_id: int, chain_id: int) -> None:
        self.id = store_id
        self.chain_id = chain_id


_STORE_ROW = {
    "id": 10,
    "store_id": STORE_ID,
    "chain_id": CHAIN_ID,
    "product_id": 7,
    "price_cents": 315,
    "was_price_cents": None,
    "unit_price_cents_per_l": None,
    "promo_kind": "none",
    "promo_qty": None,
    "promo_total_cents": None,
    "in_stock": True,
    "source": "crowd",
    "price_scope": "store",
    "observed_at": NOW - timedelta(hours=71),
}

_CHAIN_ROW = {
    "id": 11,
    "store_id": None,
    "chain_id": CHAIN_ID,
    "product_id": 7,
    "price_cents": 350,
    "was_price_cents": None,
    "unit_price_cents_per_l": None,
    "promo_kind": "none",
    "promo_qty": None,
    "promo_total_cents": None,
    "in_stock": True,
    "source": "scrape",
    "price_scope": "chain",
    "observed_at": NOW - timedelta(hours=1),
}


def test_latest_prices_still_reads_the_chain_row_when_a_store_row_exists() -> None:
    """Regression: the chain query used to be skipped for stores with any row."""
    session = _FakeSession([_STORE_ROW], [_CHAIN_ROW])
    stores = [_FakeStore(STORE_ID, CHAIN_ID)]

    prices = latest_prices(session, stores, [7], NOW - timedelta(hours=72))

    assert session.chain_queries, "chain-wide prices must be fetched for every chain"
    assert session.chain_queries[0]["chain_ids"] == [CHAIN_ID]
    assert prices[STORE_ID].primary.price_cents == 350
    assert prices[STORE_ID].fallback is not None
    assert prices[STORE_ID].fallback.price_cents == 315
