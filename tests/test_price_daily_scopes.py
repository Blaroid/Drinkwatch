"""price_daily keeps a row per store, and deal badges read the matching scope."""

from __future__ import annotations

from app.api.public import _deal_row
from app.models import PriceDaily

CHAIN = 7
STORE = 12


def _row(**overrides: object) -> dict:
    row: dict = {
        "chain_id": CHAIN,
        "store_id": None,
        "median_cents": 400.0,
        "min_cents": 350,
        "samples": 10,
    }
    row.update(overrides)
    return row


def test_store_offer_is_judged_against_its_own_history() -> None:
    own = _row(store_id=STORE, median_cents=500.0, min_cents=450)
    chain = _row(median_cents=400.0, min_cents=350)
    stats = {(CHAIN, STORE): own, (CHAIN, None): chain}

    assert _deal_row(stats, CHAIN, STORE) is own


def test_chain_wide_offer_falls_back_to_the_chain_row() -> None:
    chain = _row()
    stats = {(CHAIN, None): chain}

    assert _deal_row(stats, CHAIN, STORE) is chain


def test_thin_store_history_falls_back_to_the_chain_row() -> None:
    thin = _row(store_id=STORE, samples=2)
    chain = _row()
    stats = {(CHAIN, STORE): thin, (CHAIN, None): chain}

    assert _deal_row(stats, CHAIN, STORE) is chain


def test_no_usable_history_means_no_badge() -> None:
    assert _deal_row({}, CHAIN, STORE) is None
    assert _deal_row({(CHAIN, None): _row(samples=2)}, CHAIN, STORE) is None


def test_daily_key_covers_the_store_with_nulls_treated_as_one_value() -> None:
    table = PriceDaily.__table__
    index = next(i for i in table.indexes if i.name == "uq_price_daily_day_chain_product_store")

    assert [column.name for column in index.columns] == [
        "day",
        "chain_id",
        "product_id",
        "store_id",
    ]
    # NULL is the chain-level row, so two of them for one day must collide.
    assert index.dialect_options["postgresql"]["nulls_not_distinct"] is True


def test_table_key_matches_the_index_migration_012_builds() -> None:
    table = PriceDaily.__table__

    # A declared 3-column primary key is the drift verify_schema() now catches:
    # 012 drops it because store_id is nullable, so the table has no primary key.
    assert [column.name for column in table.primary_key.columns] == []
    # The ORM still needs an identity for the mapper, even though nothing loads
    # PriceDaily objects.
    assert [column.name for column in PriceDaily.__mapper__.primary_key] == [
        "day",
        "chain_id",
        "product_id",
    ]
