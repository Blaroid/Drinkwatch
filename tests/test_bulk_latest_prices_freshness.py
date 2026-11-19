"""Freshest-wins for the bulk path: a stale store row must not shadow a scrape.

`bulk_latest_prices()` feeds the planners (pricing.collect_offers) and alerts
(notify.evaluate_alerts), so the same rule as latest_prices() has to hold per
(store, product) key: whichever row was observed more recently wins, ties to
the store-scoped row.

Pure unit tests: no database, no network. The fake session answers the two
bulk queries and records them, which is enough to check the decision and that
the chain-wide query is always issued.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.deps import bulk_latest_prices

NOW = datetime(2026, 11, 18, 5, 20, tzinfo=timezone.utc)
CHAIN_ID = 1
OTHER_CHAIN_ID = 2
STORE_ID = 15
OTHER_STORE_ID = 16
PRODUCT_ID = 7

# The exact dict shape pricing.py / notify.py index into. If this changes, both
# callers need updating.
EXPECTED_KEYS = {
    "store_id",
    "product_id",
    "price_cents",
    "was_price_cents",
    "unit_price_cents_per_l",
    "promo_kind",
    "promo_qty",
    "promo_total_cents",
    "in_stock",
    "source",
    "price_scope",
    "observed_at",
}


def _row(
    *,
    price_cents: int,
    hours_ago: float,
    store_id: int | None = STORE_ID,
    chain_id: int = CHAIN_ID,
    source: str = "crowd",
    scope: str = "store",
    row_id: int = 1,
    product_id: int = PRODUCT_ID,
) -> dict:
    return {
        "id": row_id,
        "store_id": store_id,
        "chain_id": chain_id,
        "product_id": product_id,
        "price_cents": price_cents,
        "was_price_cents": None,
        "unit_price_cents_per_l": None,
        "promo_kind": "none",
        "promo_qty": None,
        "promo_total_cents": None,
        "in_stock": True,
        "source": source,
        "price_scope": scope,
        "observed_at": NOW - timedelta(hours=hours_ago),
    }


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict]:
        return self._rows


class _FakeSession:
    """Just enough of Session.execute to run bulk_latest_prices()' two queries."""

    def __init__(self, store_rows: list[dict], chain_rows: list[dict]) -> None:
        self.store_rows = store_rows
        self.chain_rows = chain_rows
        self.store_queries: list[dict] = []
        self.chain_queries: list[dict] = []

    def execute(self, statement, params):  # noqa: ANN001 - duck-typed test double
        # The chain-scope query is the only one that filters on a NULL store_id.
        if "o.store_id is null" in str(statement):
            self.chain_queries.append(params)
            return _FakeResult(self.chain_rows)
        self.store_queries.append(params)
        return _FakeResult(self.store_rows)


class _FakeStore:
    def __init__(self, store_id: int, chain_id: int) -> None:
        self.id = store_id
        self.chain_id = chain_id


SINCE = NOW - timedelta(hours=72)


def _call(session: _FakeSession, stores: list[_FakeStore]) -> dict:
    return bulk_latest_prices(session, stores, [PRODUCT_ID], SINCE)


def test_stale_cheap_store_row_loses_to_fresh_chain_scrape() -> None:
    """The original defect: a 71h-old $3.15 crowd row beat last night's $3.50."""
    session = _FakeSession(
        [_row(price_cents=315, hours_ago=71.0, row_id=10)],
        [_row(price_cents=350, hours_ago=1.0, store_id=None, source="scrape", scope="chain", row_id=11)],
    )

    prices = _call(session, [_FakeStore(STORE_ID, CHAIN_ID)])

    row = prices[(STORE_ID, PRODUCT_ID)]
    assert row["price_cents"] == 350
    assert row["price_scope"] == "chain"
    # A chain-wide row has store_id NULL in the ledger; the offer is stamped
    # with the store it is being attributed to.
    assert row["store_id"] == STORE_ID
    assert row["observed_at"] == NOW - timedelta(hours=1)


def test_fresh_store_sighting_still_beats_older_chain_price() -> None:
    session = _FakeSession(
        [_row(price_cents=315, hours_ago=0.5, row_id=12)],
        [_row(price_cents=350, hours_ago=20.0, store_id=None, source="scrape", scope="chain", row_id=13)],
    )

    row = _call(session, [_FakeStore(STORE_ID, CHAIN_ID)])[(STORE_ID, PRODUCT_ID)]

    assert row["price_cents"] == 315
    assert row["price_scope"] == "store"


def test_tie_prefers_the_store_scoped_row() -> None:
    """Equal timestamps: the row seen on that shelf wins over the proxy."""
    session = _FakeSession(
        [_row(price_cents=315, hours_ago=2.0, row_id=14)],
        [_row(price_cents=350, hours_ago=2.0, store_id=None, source="scrape", scope="chain", row_id=15)],
    )

    row = _call(session, [_FakeStore(STORE_ID, CHAIN_ID)])[(STORE_ID, PRODUCT_ID)]

    assert row["price_cents"] == 315
    assert row["price_scope"] == "store"


def test_naive_timestamps_do_not_break_the_comparison() -> None:
    store_row = _row(price_cents=315, hours_ago=71.0, row_id=16)
    store_row["observed_at"] = store_row["observed_at"].replace(tzinfo=None)
    session = _FakeSession(
        [store_row],
        [_row(price_cents=350, hours_ago=1.0, store_id=None, source="scrape", scope="chain", row_id=17)],
    )

    row = _call(session, [_FakeStore(STORE_ID, CHAIN_ID)])[(STORE_ID, PRODUCT_ID)]

    assert row["price_cents"] == 350


def test_store_without_its_own_row_still_gets_the_chain_price() -> None:
    session = _FakeSession(
        [],
        [_row(price_cents=350, hours_ago=1.0, store_id=None, source="scrape", scope="chain", row_id=18)],
    )

    row = _call(session, [_FakeStore(STORE_ID, CHAIN_ID)])[(STORE_ID, PRODUCT_ID)]

    assert row["price_cents"] == 350
    assert row["price_scope"] == "chain"
    assert row["store_id"] == STORE_ID


def test_pairs_with_no_rows_are_absent() -> None:
    session = _FakeSession([], [])

    assert _call(session, [_FakeStore(STORE_ID, OTHER_CHAIN_ID)]) == {}


def test_chain_query_is_issued_even_when_every_store_has_its_own_row() -> None:
    """Regression: the old rule skipped the chain row whenever a store row existed."""
    session = _FakeSession(
        [_row(price_cents=315, hours_ago=71.0, row_id=19)],
        [],
    )
    stores = [_FakeStore(STORE_ID, CHAIN_ID), _FakeStore(OTHER_STORE_ID, OTHER_CHAIN_ID)]

    _call(session, stores)

    assert session.chain_queries, "chain-wide prices must be fetched for every chain"
    assert session.chain_queries[0]["chain_ids"] == [CHAIN_ID, OTHER_CHAIN_ID]
    assert session.store_queries[0]["store_ids"] == [STORE_ID, OTHER_STORE_ID]


def test_each_store_product_pair_gets_its_own_decision() -> None:
    """One fresh store row is not a reason to skip the chain row for its neighbour."""
    session = _FakeSession(
        [_row(price_cents=315, hours_ago=0.5, row_id=20)],
        [_row(price_cents=350, hours_ago=1.0, store_id=None, source="scrape", scope="chain", row_id=21)],
    )
    stores = [_FakeStore(STORE_ID, CHAIN_ID), _FakeStore(OTHER_STORE_ID, CHAIN_ID)]

    prices = _call(session, stores)

    assert prices[(STORE_ID, PRODUCT_ID)]["price_cents"] == 315
    assert prices[(STORE_ID, PRODUCT_ID)]["price_scope"] == "store"
    assert prices[(OTHER_STORE_ID, PRODUCT_ID)]["price_cents"] == 350
    assert prices[(OTHER_STORE_ID, PRODUCT_ID)]["price_scope"] == "chain"
    assert prices[(OTHER_STORE_ID, PRODUCT_ID)]["store_id"] == OTHER_STORE_ID


def test_two_products_are_compared_independently() -> None:
    """A cheap stale row for one product must not decide the other product's row."""
    other_product_id = 8
    session = _FakeSession(
        [_row(price_cents=315, hours_ago=71.0, row_id=22)],
        [
            _row(price_cents=350, hours_ago=1.0, store_id=None, source="scrape", scope="chain", row_id=23),
            _row(
                price_cents=410,
                hours_ago=0.5,
                store_id=None,
                source="scrape",
                scope="chain",
                row_id=24,
                product_id=other_product_id,
            ),
        ],
    )

    prices = bulk_latest_prices(session, [_FakeStore(STORE_ID, CHAIN_ID)], [PRODUCT_ID, other_product_id], SINCE)

    assert prices[(STORE_ID, PRODUCT_ID)]["price_cents"] == 350
    assert prices[(STORE_ID, other_product_id)]["price_cents"] == 410


def test_returned_dict_keeps_the_shape_the_callers_index() -> None:
    session = _FakeSession(
        [_row(price_cents=315, hours_ago=0.5, row_id=25)],
        [],
    )

    row = _call(session, [_FakeStore(STORE_ID, CHAIN_ID)])[(STORE_ID, PRODUCT_ID)]

    assert set(row) == EXPECTED_KEYS
    assert row["product_id"] == PRODUCT_ID
    assert row["source"] == "crowd"


class _FakeChain:
    def __init__(self, code: str) -> None:
        self.code = code


class _FakeStoreRow:
    """A Store good enough for pricing.collect_offers()."""

    def __init__(self, store_id: int, chain_id: int) -> None:
        self.id = store_id
        self.chain_id = chain_id
        self.name = f"Store {store_id}"
        self.suburb = "Perth"
        self.chain = _FakeChain("woolworths")


class _FakeProduct:
    def __init__(self, product_id: int) -> None:
        self.id = product_id
        self.slug = "monster-ultra-500ml"
        self.brand = "Monster"
        self.name = "Ultra"
        self.variant = None
        self.size_ml = 500
        self.pack_count = 1
        self.caffeine_mg_per_100ml = 32


def test_collect_offers_uses_the_fresh_chain_price_not_the_stale_store_row() -> None:
    """The planner is the consumer: the offer it returns must be the $3.50 row."""
    from app.pricing import collect_offers

    session = _FakeSession(
        [_row(price_cents=315, hours_ago=71.0, row_id=26)],
        [_row(price_cents=350, hours_ago=1.0, store_id=None, source="scrape", scope="chain", row_id=27)],
    )

    offers = collect_offers(
        session,
        [_FakeStoreRow(STORE_ID, CHAIN_ID)],
        [_FakeProduct(PRODUCT_ID)],
        SINCE,
    )

    assert len(offers[PRODUCT_ID]) == 1
    offer = offers[PRODUCT_ID][0]
    assert offer.price_cents == 350
    assert offer.price_scope == "chain"
    assert offer.source == "scrape"
    assert offer.store_id == STORE_ID

