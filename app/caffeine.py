"""Caffeine content: curated label data, with provenance.

``products.caffeine_mg_per_100ml`` used to be written only by the admin approve
endpoint, so every scraper- or crowd-created can sat on the FSANZ 2.6.4 cap
(32 mg/100 mL) whatever the label said. This module applies the curated
``caffeine_reference`` table and records *where* a value came from, so a
fallback guess can be told apart from a printed figure.
"""

from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CaffeineReference, Product
from .scrape.normalize import canonical_brand, clean_flavour, flavour_tokens

# FSANZ Standard 2.6.4 caps formulated caffeinated beverages at 320 mg/L. That
# ceiling is the products table's fallback, not a fact about any one SKU, which
# is why caffeine_source distinguishes it from a label value.

# Marketing words that never change the caffeine content of the drink.
_EXTRA_NOISE = {"edition", "editions", "flavour", "flavours", "flavored", "flavoured"}


def caffeine_tokens(name: str, brand: str, variant: str | None = None) -> set[str]:
    """Normalised flavour tokens for matching a product against the seed table."""
    text = " ".join(filter(None, [name, variant]))
    return flavour_tokens(clean_flavour(text, brand)) - _EXTRA_NOISE


def _brand_key(value: str) -> str:
    return canonical_brand(value).strip().lower()


def reference_rows(session: Session, brand: str) -> list[CaffeineReference]:
    """Curated rows for one brand.

    The table is dozens of rows, so filtering in Python is cheaper to reason
    about than mirroring ``canonical_brand`` in SQL, and it keeps the canonical
    spelling in one place.
    """
    wanted = _brand_key(brand)
    rows = session.scalars(select(CaffeineReference)).all()
    return [row for row in rows if _brand_key(row.brand) == wanted]


def match_reference(
    rows: list[CaffeineReference], brand: str, name: str, variant: str | None = None
) -> CaffeineReference | None:
    """Most specific curated row for a product, else the brand-wide default.

    A row with an empty flavour is the brand default and only applies when no
    exact token match exists. Exact matching keeps a new flavour from silently
    inheriting a value curated for a different one; the default row exists for
    variants that share a brand's base formula.
    """
    wanted = caffeine_tokens(name, brand, variant)
    brand_key = _brand_key(brand)
    default: CaffeineReference | None = None
    for row in rows:
        if _brand_key(row.brand) != brand_key:
            continue
        key = frozenset(row.flavour.split())
        if not key:
            default = row
        elif key == wanted:
            return row
    return default


def apply_reference_caffeine(session: Session, product: Product) -> bool:
    """Fill a defaulted product from the curated table.

    Returns whether the product changed. Products already carrying a seed, OFF
    or admin value are left alone so re-running a backfill cannot overwrite a
    label value that was corrected by hand.
    """
    # SQLAlchemy's Python-side default is applied at flush, so a product built
    # in memory still reads None here; both mean "no value chosen yet".
    if product.caffeine_source not in (None, "default"):
        return False
    row = match_reference(
        reference_rows(session, product.brand), product.brand, product.name, product.variant
    )
    if row is None:
        return False
    product.caffeine_mg_per_100ml = float(row.caffeine_mg_per_100ml)
    product.caffeine_source = "seed"
    return True


def plausible_caffeine(value: object) -> float | None:
    """A per-100 mL figure a drink can actually have, or None.

    Open Food Facts is crowd-edited and sometimes stores a powder's value as if
    it were a drink; anything outside 0-200 mg/100 mL is a data error, not a
    caffeine content we should put in the ledger.
    """
    try:
        mg = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(mg) or mg < 0 or mg > 200:
        return None
    return round(mg, 1)


def caffeine_from_off(caffeine_100g: object) -> float | None:
    """Convert Open Food Facts ``caffeine_100g`` (grams/100 g) to mg/100 mL."""
    if caffeine_100g is None:
        return None
    try:
        grams = float(caffeine_100g)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return plausible_caffeine(grams * 1000)
