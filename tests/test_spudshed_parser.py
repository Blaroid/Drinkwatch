"""Parser tests for the SpudShed scraper, driven by a saved synthetic page.

The fixture is hand-written markup in the shape of a SpudShed category page
(talker tiles), not a captured response: the tests must never depend on a live
retailer fetch, and the shapes worth pinning are the awkward ones (missing title
attribute, stale "was" price, incomplete tiles).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.scrape.normalize import parse_pack
from app.scrape.spudshed import parse_items, store_base_url

FIXTURE = Path(__file__).parent / "fixtures" / "spudshed_energy.html"
BASE_URL = "https://willetton.spudshed.com.au"


def load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@dataclass
class StubStore:
    """Only the two attributes store_base_url reads; keeps the test ORM-free."""

    website: str | None = None
    external_ref: str | None = None


def test_parses_every_complete_tile() -> None:
    items = parse_items(load_fixture(), BASE_URL)

    assert [(item.line_id, item.title, item.price_cents) for item in items] == [
        ("4821", "Monster Aussie Lemonade 500mL", 329),
        ("4822", "Red Bull Energy Drink 250mL", 310),
        ("4823", "V Energy 500mL", 350),
        ("4824", "Mother 500mL", 350),
    ]


def test_joins_listing_urls_onto_the_store_base() -> None:
    items = parse_items(load_fixture(), BASE_URL)

    assert items[0].url == f"{BASE_URL}/lines/monster-aussie-lemonade-500ml"
    assert all(item.url.startswith(f"{BASE_URL}/lines/") for item in items)


def test_reads_the_was_price_from_the_strike_through_label() -> None:
    items = {item.line_id: item for item in parse_items(load_fixture(), BASE_URL)}

    assert items["4821"].was_price_cents == 449
    assert items["4821"].size_text == "500mL"


def test_ignores_a_was_price_below_the_sell_price() -> None:
    # Stale markup after a price rise: $2.00 "was" is not a special on $3.50.
    items = {item.line_id: item for item in parse_items(load_fixture(), BASE_URL)}

    assert items["4824"].price_cents == 350
    assert items["4824"].was_price_cents is None


def test_falls_back_to_the_name_spans_when_the_title_attribute_is_missing() -> None:
    items = {item.line_id: item for item in parse_items(load_fixture(), BASE_URL)}

    # 4822 has no title attribute at all, 4823 has an empty one; both are rebuilt
    # from talker__product-name + talker__name__size.
    assert items["4822"].title == "Red Bull Energy Drink 250mL"
    assert items["4823"].title == "V Energy 500mL"


def test_skips_tiles_without_an_id_link_or_price() -> None:
    items = parse_items(load_fixture(), BASE_URL)

    # promo_9001 is not a line_ tile; 4825 has no product link; 4826 has no sell price.
    assert {item.line_id for item in items}.isdisjoint({"promo_9001", "4825", "4826"})
    assert len(items) == 4


def test_parsed_titles_carry_a_usable_pack_size() -> None:
    items = parse_items(load_fixture(), BASE_URL)

    assert [parse_pack(item.title) for item in items] == [(500, 1), (250, 1), (500, 1), (500, 1)]


@pytest.mark.parametrize(
    ("store", "expected"),
    [
        (StubStore(website="https://willetton.spudshed.com.au/"), "https://willetton.spudshed.com.au"),
        (StubStore(external_ref="willetton.spudshed.com.au"), "https://willetton.spudshed.com.au"),
        # A bare slug is not a host; storing it as one would build a broken URL.
        (StubStore(external_ref="willetton"), None),
        # A scheme means it is not a bare subdomain either.
        (StubStore(external_ref="https://willetton.spudshed.com.au"), None),
        (StubStore(), None),
    ],
)
def test_store_base_url(store: StubStore, expected: str | None) -> None:
    assert store_base_url(store) == expected  # type: ignore[arg-type]
