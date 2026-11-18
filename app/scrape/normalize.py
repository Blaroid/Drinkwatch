"""Turn messy retailer product names into canonical catalogue rows."""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Product

SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|mL|ML|l|L|litres?|liters?)\b")
PACK_X_RE = re.compile(r"\b(\d{1,2})\s*[x×]\s*(\d+(?:\.\d+)?)\s*(ml|mL|ML|l|L)\b")
PACK_WORD_RE = re.compile(r"\b(\d{1,2})\s*(?:pack|pk|p)\b", re.IGNORECASE)
# "(24x Pack)" / "24x carton": count before an "x", no per-unit size.
PACK_COUNT_X_RE = re.compile(r"\b(\d{1,2})\s*[x×]\s*(?:pack|pk|carton|box)\b", re.IGNORECASE)
# A token that is just a count ("24", "24x") is never a flavour.
COUNT_TOKEN_RE = re.compile(r"^\d+x?$")

MONEY_RE = re.compile(r"\$?\s*(\d{1,4}(?:\.\d{2})?)")

# Brand detection: ordered, longest first. Canonical spelling on the right.
BRAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bred\s*bull\b", re.I), "Red Bull"),
    (re.compile(r"\bcoca[\s-]*cola\s*energy\b", re.I), "Coca-Cola Energy"),
    (re.compile(r"\bcoke\s*energy\b", re.I), "Coca-Cola Energy"),
    (re.compile(r"\bmonster\b", re.I), "Monster"),
    (re.compile(r"\bmother\b", re.I), "Mother"),
    (re.compile(r"\brockstar\b", re.I), "Rockstar"),
    (re.compile(r"\bdare\b", re.I), "Dare"),
    (re.compile(r"\bnos\b", re.I), "NOS"),
    (re.compile(r"\bprime\b", re.I), "Prime"),
    (re.compile(r"\bg\s*fuel\b", re.I), "G Fuel"),
    (re.compile(r"\breign\b", re.I), "Reign"),
    (re.compile(r"\bcelsius\b", re.I), "Celsius"),
    (re.compile(r"\bbang\b", re.I), "Bang"),
    (re.compile(r"\bxs\s*energy\b", re.I), "XS"),
    (re.compile(r"\bv\s*energy\b", re.I), "V"),
    (re.compile(r"\bvodka\s*energy\b", re.I), "Vodka Energy"),
    (re.compile(r"\brelentless\b", re.I), "Relentless"),
    (re.compile(r"\b3d\s*energy\b", re.I), "3D"),
    (re.compile(r"\bbpm\b", re.I), "BPM"),
]

# Brands that only ever sell energy drinks (fast accept).
STRICT_BRANDS = {
    "Monster", "Red Bull", "Mother", "Rockstar", "V", "NOS", "Reign", "Bang",
    "Celsius", "XS", "Relentless", "3D", "BPM", "Coca-Cola Energy", "G Fuel",
    "Vodka Energy",
}
# Brands with both energy and non-energy ranges: require an energy word.
LOOSE_BRANDS = {"Dare", "Prime"}

# Brands that share the "energy" search keywords but are not energy drinks.
CATEGORY_BY_BRAND = {"Dare": "iced-coffee", "Prime": "hydration"}
ENERGY_WORD = re.compile(r"\benergy\b|\biced coffee\b|\bhydration\b", re.I)

NOISE_TOKENS = {
    "energy", "drink", "drinks", "can", "cans", "bottle", "bottles", "each", "pack",
    "pk", "multipack", "multi", "single", "the", "of", "and", "with", "plus", "carton",
    "x", "ml", "l", "litre", "litres", "liter", "liters", "sugarfree", "sugar",
}


def parse_money(text: str | None) -> int | None:
    if not text:
        return None
    match = MONEY_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        return round(float(match.group(1)) * 100)
    except ValueError:
        return None


def parse_pack(text: str | None) -> tuple[int | None, int]:
    """Return (size_ml_per_unit, pack_count) from strings like '500mL x 4 pack' or '2L'."""
    if not text:
        return None, 1
    pack_count = 1
    size_ml: int | None = None

    multi = PACK_X_RE.search(text)
    if multi:
        pack_count = int(multi.group(1))
        size_ml = _to_ml(float(multi.group(2)), multi.group(3))
    elif PACK_COUNT_X_RE.search(text):
        pack_count = int(PACK_COUNT_X_RE.search(text).group(1))
        single = SIZE_RE.search(text)
        if single:
            size_ml = _to_ml(float(single.group(1)), single.group(2))
    else:
        single = SIZE_RE.search(text)
        if single:
            size_ml = _to_ml(float(single.group(1)), single.group(2))
        word = PACK_WORD_RE.search(text)
        if word:
            pack_count = int(word.group(1))
    return size_ml, max(1, pack_count)


def _to_ml(value: float, unit: str) -> int | None:
    unit = unit.lower()
    if unit.startswith("l"):
        return int(round(value * 1000))
    return int(round(value))


# Retailers spell the same brand differently ("V Energy", "Red Bull Energy Drink").
BRAND_CANON = {
    "v": "V",
    "v energy": "V",
    "v energy drink": "V",
    "red bull": "Red Bull",
    "redbull": "Red Bull",
    "red bull energy drink": "Red Bull",
    "monster energy": "Monster",
    "monster energy drink": "Monster",
    "mother energy": "Mother",
    "mother energy drink": "Mother",
    "rockstar energy": "Rockstar",
    "nos energy": "NOS",
    "dare iced coffee": "Dare",
    "coca cola energy": "Coca-Cola Energy",
    "coke energy": "Coca-Cola Energy",
    "g fuel": "G Fuel",
    "gfuel": "G Fuel",
}


def canonical_brand(brand: str) -> str:
    cleaned = re.sub(r"\s+", " ", brand.strip())
    key = cleaned.lower()
    if key in BRAND_CANON:
        return BRAND_CANON[key]
    trimmed = re.sub(r"\s+(energy\s+drink|energy|drinks)$", "", key).strip()
    return BRAND_CANON.get(trimmed, cleaned)


def detect_brand(name: str) -> str | None:
    for pattern, brand in BRAND_PATTERNS:
        if pattern.search(name):
            return brand
    return None


def classify_category(brand: str | None, name: str) -> str:
    if brand in CATEGORY_BY_BRAND:
        return CATEGORY_BY_BRAND[brand]
    if re.search(r"\biced coffee\b", name, re.I):
        return "iced-coffee"
    return "energy"


def is_energy_item(name: str, brand: str | None = None, size_ml: int | None = None) -> bool:
    """Decide whether a retailer search hit is really an energy drink.

    Search on supermarket sites is fuzzy: "monster energy" also returns Monster
    branded cables and unrelated supplements. Requiring a pack size plus an
    energy brand (or an explicit energy word) filters those out.
    """
    if not size_ml:
        return False
    brand = brand if brand in STRICT_BRANDS or brand in LOOSE_BRANDS else detect_brand(name)
    if brand is None:
        return False
    if brand in STRICT_BRANDS:
        return True
    if brand in LOOSE_BRANDS:
        return bool(ENERGY_WORD.search(name))
    return False


def flavour_tokens(text: str) -> set[str]:
    cleaned = unicodedata.normalize("NFKD", text.lower())
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    tokens = {t for t in cleaned.split() if t and not t.isdigit()}
    return {t for t in tokens if t not in NOISE_TOKENS}


def clean_flavour(name: str, brand: str | None) -> str:
    text = name
    if brand:
        text = re.sub(re.escape(brand), " ", text, flags=re.I)
    text = PACK_X_RE.sub(" ", text)
    text = SIZE_RE.sub(" ", text)
    text = re.sub(r"\b\d+\s*(?:pack|pk|p)\b", " ", text, flags=re.I)
    text = re.sub(r"[^A-Za-z0-9\s'-]", " ", text)
    words = [
        w for w in text.split()
        if w.lower() not in NOISE_TOKENS and not COUNT_TOKEN_RE.match(w)
    ]
    return " ".join(words).strip(" -")


def normalize_barcode(raw: str | None) -> str | None:
    """Canonical 13-digit barcode.

    Retailers hand out GTIN-14 (Woolworths prefixes 13-digit EANs with a packaging
    "1"), UPC-A 12-digit codes, and the odd internal synthetic code. Normalise to
    EAN-13 so the same can always matches itself.
    """
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", str(raw))
    if not digits:
        return None
    if len(digits) == 14:
        if digits[0] in {"1", "0"}:
            digits = digits[1:]
        else:
            digits = digits.lstrip("0")
    if len(digits) == 12:
        digits = "0" + digits
    if len(digits) > 13:
        digits = digits.lstrip("0")
    return digits if 8 <= len(digits) <= 13 else None


def barcode_variants(raw: str | None) -> list[str]:
    """Every plausible key for one physical product code, most canonical first.

    Woolworths hands out GTIN-14 strings whose check digit does not survive
    stripping the packaging indicator, and some codes are retailer inventions.
    Indexing all forms keeps matching lossless.
    """
    if not raw:
        return []
    digits = re.sub(r"[^0-9]", "", str(raw))
    if not digits:
        return []
    variants: list[str] = [digits]
    if len(digits) == 14 and digits[0] in {"1", "0"}:
        variants.append(digits[1:])
        variants.append(digits[1:].lstrip("0") or digits[1:])
    if len(digits) == 13:
        variants.append("0" + digits)
    if len(digits) == 12:
        variants.append("0" + digits)
    if digits.startswith("0"):
        variants.append(digits.lstrip("0"))
    seen: list[str] = []
    for candidate in variants:
        if 8 <= len(candidate) <= 14 and candidate not in seen:
            seen.append(candidate)
    return seen


def register_barcode(session, product_id: int, raw: str | None, source: str = "scrape") -> int:
    """Index every variant of a code against a product (idempotent)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from ..models import Barcode

    written = 0
    for variant in barcode_variants(raw):
        statement = (
            pg_insert(Barcode)
            .values(barcode=variant, product_id=product_id, source=source)
            .on_conflict_do_nothing(index_elements=["barcode"])
        )
        written += 1 if session.execute(statement).rowcount else 0
    return written


def ean13_checksum_ok(barcode: str | None) -> bool:
    """Validate the mod-10 check digit (8/12/13/14 digit GTINs)."""
    if not barcode or not barcode.isdigit():
        return False
    body, check = barcode[:-1], int(barcode[-1])
    total = 0
    for index, char in enumerate(reversed(body)):
        total += int(char) * (3 if index % 2 == 0 else 1)
    return (10 - total % 10) % 10 == check


def family_slug_for(slug: str) -> str:
    """monster-ultra-500ml-x4 -> monster-ultra-500ml (pack size folded away)."""
    return re.sub(r"-x[0-9]+$", "", slug)


def caffeine_mg_for(product) -> float:
    """Caffeine in one unit (can/bottle) of a product, in mg."""
    per_100 = float(product.caffeine_mg_per_100ml or 0)
    if not product.size_ml:
        return 0.0
    return per_100 * product.size_ml / 100.0


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def build_slug(
    brand: str, name: str, variant: str | None, size_ml: int | None, pack_count: int
) -> str:
    flavour = clean_flavour(" ".join(filter(None, [name, variant])), brand)
    parts = [slugify(brand)]
    if flavour:
        parts.append(slugify(flavour))
    if size_ml:
        parts.append(f"{size_ml}ml")
    if pack_count and pack_count > 1:
        parts.append(f"x{pack_count}")
    return "-".join(parts)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_product(
    session: Session,
    *,
    name: str,
    brand: str | None = None,
    size_ml: int | None = None,
    pack_count: int = 1,
    barcode: str | None = None,
    category: str = "energy",
    create: bool = True,
) -> tuple[Product | None, bool]:
    """Find (or create) the canonical product for a retailer listing.

    Returns (product, created).
    """
    if barcode:
        from ..models import Barcode

        hit = session.scalar(
            select(Product)
            .join(Barcode, Barcode.product_id == Product.id)
            .where(Barcode.barcode.in_(barcode_variants(barcode)))
        )
        if hit is not None:
            return hit, False

    brand = canonical_brand(brand) if brand else detect_brand(name)
    if brand is None:
        return None, False

    candidates = list(
        session.scalars(
            select(Product).where(
                func.lower(Product.brand) == brand.lower(),
                Product.size_ml == size_ml,
                Product.pack_count == pack_count,
            )
        )
    )
    wanted = flavour_tokens(clean_flavour(name, brand))
    scored: list[tuple[float, Product]] = []
    for candidate in candidates:
        have = flavour_tokens(
            " ".join(filter(None, [candidate.name, candidate.variant]))
        )
        scored.append((_jaccard(wanted, have), candidate))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        if scored[0][0] >= 0.5 or (not wanted and scored[0][0] >= 0.5):
            return scored[0][1], False

    if not create:
        return None, False

    # Never fall back to the raw retailer title ("Red Bull Energy Drink Can
    # 250mL"): with the brand and size stripped there is nothing left, and
    # "original" is what those products actually are.
    flavour = clean_flavour(name, brand) or "Original"
    slug = build_slug(brand, flavour, None, size_ml, pack_count)
    existing = session.scalar(select(Product).where(Product.slug == slug))
    if existing is not None:
        return existing, False

    barcode_norm = normalize_barcode(barcode)
    product = Product(
        slug=slug,
        brand=brand,
        name=flavour,
        variant=None,
        size_ml=size_ml,
        pack_count=pack_count,
        barcode=barcode,
        normalised_barcode=barcode_norm,
        family_slug=family_slug_for(slug),
        category=classify_category(brand, name) if category == "energy" else category,
        source="auto",
        status="verified",
    )
    session.add(product)
    session.flush()
    if barcode:
        register_barcode(session, product.id, barcode, source="scrape")
    return product, True


def per_litre_cents(price_cents: int, size_ml: int | None, pack_count: int) -> int | None:
    if not size_ml or not pack_count:
        return None
    litres = size_ml * pack_count / 1000
    if litres <= 0:
        return None
    return round(price_cents / litres)
