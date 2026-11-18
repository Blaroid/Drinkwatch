"""Caffeine curation: flavour matching and Open Food Facts conversion.

Pure unit tests: no database and no network, so they run anywhere the shared
venv has pytest.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.caffeine import (
    caffeine_from_off,
    caffeine_tokens,
    match_reference,
    plausible_caffeine,
)
from app.models import CaffeineReference


class _StubSession:
    """Just enough of a Session for barcode._cached(), which only calls scalar()."""

    def __init__(self, payload: dict | None) -> None:
        self._payload = payload

    def scalar(self, _statement: object) -> object | None:
        if self._payload is None:
            return None
        return SimpleNamespace(payload=self._payload)


def _row(brand: str, flavour: str, mg: float = 32.0) -> CaffeineReference:
    # Not persisted: match_reference only reads the four fields.
    return CaffeineReference(
        brand=brand, flavour=flavour, caffeine_mg_per_100ml=mg, source="test"
    )


def test_tokens_drop_brand_size_and_marketing_words() -> None:
    assert caffeine_tokens("Monster Ultra Watermelon 500mL", "Monster") == {
        "ultra",
        "watermelon",
    }


def test_edition_word_does_not_block_a_flavour_row() -> None:
    rows = [_row("Red Bull", "watermelon")]
    match = match_reference(rows, "Red Bull", "Watermelon Edition", None)
    assert match is not None and match.flavour == "watermelon"


def test_exact_flavour_beats_the_brand_default() -> None:
    rows = [_row("Monster", "", 32.0), _row("Monster", "java mean bean", 42.34)]
    match = match_reference(rows, "Monster", "Java Mean Bean", None)
    assert match is not None and float(match.caffeine_mg_per_100ml) == 42.34


def test_brand_default_covers_an_unlisted_flavour() -> None:
    rows = [_row("V", "", 31.2)]
    match = match_reference(rows, "V", "Blue", None)
    assert match is not None and float(match.caffeine_mg_per_100ml) == 31.2


def test_brand_default_is_not_shared_across_brands() -> None:
    rows = [_row("Monster", "", 32.0)]
    assert match_reference(rows, "Mother", "Original", None) is None


def test_brand_spellings_are_canonicalised() -> None:
    rows = [_row("Monster", "ultra")]
    assert match_reference(rows, "Monster Energy", "Ultra", None) is not None


def test_off_caffeine_is_converted_from_grams_per_100g() -> None:
    assert caffeine_from_off(0.032) == 32.0
    assert caffeine_from_off("0.078") == 78.0
    assert caffeine_from_off(0) == 0.0
    assert caffeine_from_off(None) is None


def test_implausible_caffeine_is_rejected() -> None:
    # 1.5 g/100 g is a caffeine powder, not a drink; -1 and >200 are corrupt.
    assert caffeine_from_off(1.5) is None
    assert plausible_caffeine(-1) is None
    assert plausible_caffeine(201) is None
    assert plausible_caffeine("not a number") is None


def test_cached_off_payload_supplies_a_label_value() -> None:
    from app.api.barcode import _cached_caffeine

    payload = {
        "source": "openfoodfacts",
        "found": True,
        "caffeine_100g": 0.032,
        "caffeine_mg_per_100ml": 32.0,
    }
    assert _cached_caffeine(_StubSession(payload), "9300000000000") == 32.0


def test_cached_miss_or_bad_value_supplies_nothing() -> None:
    from app.api.barcode import _cached_caffeine

    negative = {"source": "openfoodfacts", "found": False}
    assert _cached_caffeine(_StubSession(negative), "9300000000000") is None
    assert _cached_caffeine(_StubSession(None), "9300000000000") is None
    corrupt = {"source": "openfoodfacts", "found": True, "caffeine_mg_per_100ml": 900.0}
    assert _cached_caffeine(_StubSession(corrupt), "9300000000000") is None
