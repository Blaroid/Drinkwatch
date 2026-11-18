"""Unit tests for the string/code normalisers the scrapers feed into the catalogue.

Every case is a literal string: no fixtures, no database, no retailer calls.
"""

from __future__ import annotations

import pytest

from app.scrape.normalize import barcode_variants, normalize_barcode, parse_pack


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, (None, 1)),
        ("", (None, 1)),
        ("Monster Aussie Lemonade 500mL", (500, 1)),
        ("600 ml", (600, 1)),
        ("2L", (2000, 1)),
        ("1.25L", (1250, 1)),
        # Count first, size second: "4 x 500mL".
        ("4 x 500mL", (500, 4)),
        ("12 x 250ml carton", (250, 12)),
        # Size first, count spelled out: "500mL x 4 pack".
        ("500mL x 4 pack", (500, 4)),
        ("375ml cans 10pk", (375, 10)),
        # A bare count with no per-unit size, e.g. a carton line.
        ("(24x Pack)", (None, 24)),
        ("24 pack", (None, 24)),
    ],
)
def test_parse_pack(text: str | None, expected: tuple[int | None, int]) -> None:
    assert parse_pack(text) == expected


def test_parse_pack_treats_a_missing_count_as_a_single() -> None:
    assert parse_pack("V Energy 500mL single") == (500, 1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("abc", None),
        # Already EAN-13.
        ("9300675001234", "9300675001234"),
        # Woolworths hands out a GTIN-14 with a "1" packaging indicator.
        ("19300675001234", "9300675001234"),
        # Spaces and hyphens are typing noise, not digits.
        (" 9300-675-001234 ", "9300675001234"),
        # UPC-A 12-digit becomes EAN-13 with a leading zero.
        ("930067500123", "0930067500123"),
        # 8-digit codes (EAN-8) are kept as-is.
        ("12345678", "12345678"),
        # Too short to be a retail barcode.
        ("1234567", None),
    ],
)
def test_normalize_barcode(raw: str | None, expected: str | None) -> None:
    assert normalize_barcode(raw) == expected


def test_normalize_barcode_rejects_an_unstrippable_gtin14() -> None:
    # A packaging indicator other than 0/1 cannot be folded to EAN-13 by digit
    # stripping alone. Returning None is deliberate: a wrong 13-digit code would
    # alias two different products. barcode_variants still keeps the raw key.
    assert normalize_barcode("29300675001234") is None
    assert barcode_variants("29300675001234") == ["29300675001234"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("no digits here", []),
        ("1234567", []),
        # Woolworths GTIN-14: raw code first, then the EAN-13 payload.
        ("19300675001234", ["19300675001234", "9300675001234"]),
        # EAN-13 also gets the zero-padded GTIN-14 spelling.
        ("9300675001234", ["9300675001234", "09300675001234"]),
        # UPC-A gets both its 12- and 13-digit spellings.
        ("930067500123", ["930067500123", "0930067500123"]),
        # A leading zero is also indexed without it.
        (
            "0123456789012",
            ["0123456789012", "00123456789012", "123456789012"],
        ),
        # Formatting is stripped before variants are generated.
        (" 9300-675-001234 ", ["9300675001234", "09300675001234"]),
    ],
)
def test_barcode_variants(raw: str | None, expected: list[str]) -> None:
    assert barcode_variants(raw) == expected


def test_barcode_variants_are_unique_and_ordered_most_canonical_first() -> None:
    variants = barcode_variants("19300675001234")

    assert len(variants) == len(set(variants))
    assert variants[0] == "19300675001234"
