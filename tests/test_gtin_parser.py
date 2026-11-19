"""Parser tests for the product-detail GTIN sources.

The saved responses are real shapes: a Woolworths
``/apis/ui/product/detail/<stockcode>`` payload and a Coles ``/product/<id>``
page carrying both its schema.org JSON-LD and the Next.js ``pageProps`` blob.
The search payloads of both chains have a ``Barcode`` key that is null on every
tile, so these detail shapes are the only place a scan can get a code from.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.scrape.gtin import (
    coles_page_has_product,
    memo_key,
    parse_coles_product,
    parse_woolworths_detail,
    plausible_gtin,
)

FIXTURES = Path(__file__).parent / "fixtures"
WOOLLIES = FIXTURES / "woolworths_product_detail.json"
COLES = FIXTURES / "coles_product.html"


def test_reads_the_gtin_from_a_woolworths_detail_payload() -> None:
    payload = json.loads(WOOLLIES.read_text(encoding="utf-8"))

    assert parse_woolworths_detail(payload) == "9342866000215"


def test_woolworths_detail_without_a_barcode_is_not_a_code() -> None:
    assert parse_woolworths_detail({"Product": {"Stockcode": 1142378818, "Barcode": None}}) is None
    assert parse_woolworths_detail({"Product": {"Barcode": ""}}) is None
    assert parse_woolworths_detail({"Product": {}}) is None
    assert parse_woolworths_detail({}) is None


def test_woolworths_detail_rejects_a_bad_check_digit() -> None:
    # 9342866000215 is the real code; one digit off is exactly the seeded noise
    # the v0.5.2 purge removed.
    assert parse_woolworths_detail({"Product": {"Barcode": "9342866000216"}}) is None


def test_reads_the_gtin_from_the_coles_json_ld() -> None:
    html = COLES.read_text(encoding="utf-8")

    assert parse_coles_product(html) == "9002490100490"


def test_coles_falls_back_to_the_next_data_blob() -> None:
    html = COLES.read_text(encoding="utf-8")
    without_json_ld = re.sub(
        r'<script type="application/ld\+json">.*?</script>', "", html, flags=re.S
    )

    assert "application/ld+json" not in without_json_ld
    assert parse_coles_product(without_json_ld) == "9002490100490"


def test_coles_product_without_a_gtin_is_not_a_code() -> None:
    payload = {
        "props": {"pageProps": {"product": {"id": 1, "name": "No code", "gtin": None}}}
    }
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></body></html>"
    )

    assert parse_coles_product(html) is None
    assert parse_coles_product("<html></html>") is None
    assert parse_coles_product("<script>not json</script>") is None


def test_coles_page_without_a_product_is_not_mistaken_for_a_missing_gtin() -> None:
    # Akamai serves a challenge with HTTP 200. "ewelcome-" also appears on valid
    # pages, so the only safe signal is whether the product payload exists.
    challenge = '<html><head><script src="/ewelcome-abc.js"></script></head></html>'
    empty_next = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{}}}</script></body></html>'
    )

    assert coles_page_has_product(COLES.read_text(encoding="utf-8")) is True
    assert coles_page_has_product(challenge) is False
    assert coles_page_has_product(empty_next) is False


def test_gtin_checksum_is_checked_before_normalising() -> None:
    # A Woolworths GTIN-14's check digit covers the packaging indicator; the
    # stripped 13-digit form fails the checksum even though the retailer code is
    # genuine. Validate the retailer form, then let register_barcode index both.
    assert plausible_gtin("19342866002025") == "19342866002025"
    assert plausible_gtin("9342866002025") is None
    # A retailer-internal 10-digit SKU is not a GTIN, even if it has a valid
    # mod-10 digit by accident.
    assert plausible_gtin("1234567890") is None


def test_memo_keys_fit_the_barcode_cache_primary_key() -> None:
    assert memo_key("woolworths", "1142378818") == "ww:1142378818"
    assert memo_key("coles", "2785108") == "coles:2785108"
    assert len(memo_key("woolworths", "9" * 20)) <= 20
