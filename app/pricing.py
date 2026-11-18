"""Price maths shared by the basket, budget and route endpoints.

Nothing here talks HTTP: give it offers, get plans back. Kept pure so the
optimisers can be reasoned about (and unit-tested) on their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .api.deps import bulk_latest_prices
from .config import settings
from .models import Product, Store


@dataclass(frozen=True)
class Bundle:
    """A purchasable option: n cans for c cents."""

    units: int
    cost_cents: int
    caffeine_mg: float
    label: str


@dataclass
class Offer:
    product_id: int
    slug: str
    label: str
    store_id: int
    store_name: str
    chain_code: str
    suburb: str | None
    distance_km: float
    price_cents: int
    was_price_cents: int | None
    promo_kind: str
    promo_qty: int | None
    promo_total_cents: int | None
    size_ml: int | None
    pack_count: int
    caffeine_mg_per_100ml: float
    price_scope: str
    source: str
    observed_at: datetime

    @property
    def can_caffeine_mg(self) -> float:
        if not self.size_ml:
            return 0.0
        return self.caffeine_mg_per_100ml * self.size_ml / 100.0

    @property
    def pack_caffeine_mg(self) -> float:
        return self.can_caffeine_mg * self.pack_count

    def bundles(self) -> list[Bundle]:
        options = [Bundle(1, self.price_cents, self.can_caffeine_mg, "single")]
        if self.pack_count > 1:
            options.append(
                Bundle(self.pack_count, self.price_cents, self.pack_caffeine_mg, f"{self.pack_count}-pack")
            )
        if self.promo_kind == "multibuy" and self.promo_qty and self.promo_total_cents:
            units = self.pack_count * self.promo_qty
            options.append(
                Bundle(
                    units,
                    self.promo_total_cents,
                    self.pack_caffeine_mg * self.promo_qty,
                    f"{self.promo_qty} x {self.pack_count}-pack deal",
                )
            )
        return options


def product_label(product: Product) -> str:
    bits = [product.brand, product.name]
    if product.variant:
        bits.append(product.variant)
    label = " ".join(bit for bit in bits if bit).strip()
    if product.size_ml:
        label += f" {product.size_ml}ml"
        if product.pack_count > 1:
            label += f" x{product.pack_count}"
    return label


def collect_offers(
    session: Session,
    stores: list[Store],
    products: list[Product],
    since: datetime,
    distances: dict[int, float] | None = None,
) -> dict[int, list[Offer]]:
    """Latest price for each product at each store, keyed by product id."""
    by_store = {store.id: store for store in stores}
    distances = distances or {}
    product_by_id = {product.id: product for product in products}
    prices = bulk_latest_prices(session, stores, list(product_by_id), since)
    result: dict[int, list[Offer]] = {product.id: [] for product in products}
    for (store_id, product_id), obs in prices.items():
        store = by_store.get(store_id)
        product = product_by_id.get(product_id)
        if store is None or product is None:
            continue
        if obs["price_cents"] > 0:
            result[product.id].append(
                Offer(
                    product_id=product.id,
                    slug=product.slug,
                    label=product_label(product),
                    store_id=store.id,
                    store_name=store.name,
                    chain_code=store.chain.code,
                    suburb=store.suburb,
                    distance_km=distances.get(store.id, 0.0),
                    price_cents=obs["price_cents"],
                    was_price_cents=obs.get("was_price_cents"),
                    promo_kind=obs.get("promo_kind") or "none",
                    promo_qty=obs.get("promo_qty"),
                    promo_total_cents=obs.get("promo_total_cents"),
                    size_ml=product.size_ml,
                    pack_count=product.pack_count,
                    caffeine_mg_per_100ml=float(product.caffeine_mg_per_100ml or 0),
                    price_scope=obs.get("price_scope") or "store",
                    source=obs.get("source") or "scrape",
                    observed_at=obs["observed_at"],
                )
            )
    for offers in result.values():
        offers.sort(key=lambda offer: offer.price_cents)
    return result


def family_products(session: Session, key: str) -> list[Product]:
    """Every pack size / retailer spelling of one drink.

    Starts from the family_slug (pack size folded away), then pulls in rows that
    are linked to the family through products.canonical_product_id, so a curated
    cross-chain link also merges the bulk-pack comparison.
    """
    exact = session.scalar(select(Product).where(Product.slug == key))
    family = exact.family_slug if exact is not None and exact.family_slug else key
    base = list(
        session.scalars(
            select(Product).where(Product.family_slug == family, Product.status != "rejected")
        )
    )
    if not base and exact is not None:
        base = [exact]

    roots = set()
    for product in base:
        roots.add(product.id)
        if product.canonical_product_id:
            roots.add(product.canonical_product_id)
    aliases = list(
        session.scalars(
            select(Product).where(Product.status != "rejected", Product.canonical_product_id.in_(roots))
        )
    )
    linked_roots = list(
        session.scalars(
            select(Product).where(Product.status != "rejected", Product.id.in_(
                [product.canonical_product_id for product in base if product.canonical_product_id]
            ))
        )
    )
    merged: dict[int, Product] = {product.id: product for product in base}
    for product in aliases + linked_roots:
        merged.setdefault(product.id, product)
    return list(merged.values())


def prune_bundles(bundles: list[Bundle]) -> list[Bundle]:
    """Drop dominated options: cheaper or equal cost for at least as many cans."""
    ordered = sorted(bundles, key=lambda bundle: (bundle.units, bundle.cost_cents))
    kept: list[Bundle] = []
    for bundle in ordered:
        dominated = False
        for other in kept:
            if other.units <= bundle.units and other.cost_cents <= bundle.cost_cents:
                if other.units < bundle.units or other.cost_cents < bundle.cost_cents:
                    dominated = True
                    break
        if not dominated:
            kept.append(bundle)
    return kept


def cheapest_quantity_plan(bundles: list[Bundle], quantity: int) -> tuple[int, list[tuple[Bundle, int]]] | None:
    """Cheapest way to buy at least 'quantity' cans (integer coin change).

    Returns (total_cents, [(bundle, count), ...]) or None when nothing can reach it.
    """
    if quantity <= 0:
        return 0, []
    options = prune_bundles(bundles)
    if not options:
        return None
    infinity = 10**9
    best = [infinity] * (quantity + 1)
    pick: list[Bundle | None] = [None] * (quantity + 1)
    best[0] = 0
    for need in range(1, quantity + 1):
        for bundle in options:
            previous = max(0, need - bundle.units)
            candidate = best[previous] + bundle.cost_cents
            if candidate < best[need]:
                best[need] = candidate
                pick[need] = bundle
    if best[quantity] >= infinity:
        return None
    counts: dict[Bundle, int] = {}
    need = quantity
    while need > 0:
        bundle = pick[need]
        if bundle is None:  # pragma: no cover - defensive
            return None
        counts[bundle] = counts.get(bundle, 0) + 1
        need = max(0, need - bundle.units)
    return best[quantity], list(counts.items())


@dataclass
class KnapsackItem:
    cost_cents: int
    caffeine_mg: float
    label: str
    store_id: int
    store_name: str
    chain_code: str
    slug: str
    units: int


@dataclass
class KnapsackResult:
    spent_cents: int = 0
    caffeine_mg: float = 0.0
    items: list[tuple[KnapsackItem, int]] = field(default_factory=list)


def maximise_caffeine(items: list[KnapsackItem], budget_cents: int) -> KnapsackResult:
    """Unbounded integer knapsack: most caffeine for the money, exact.

    Budget is in cents so the DP table stays small (<= 20000 entries). Items are
    pre-pruned by dominance to keep the inner loop short.
    """
    usable = [item for item in items if item.cost_cents > 0 and item.caffeine_mg > 0]
    if not usable or budget_cents <= 0:
        return KnapsackResult()

    # Dominance: keep the best mg-per-cent options and drop strictly worse ones.
    usable.sort(key=lambda item: (item.cost_cents, -item.caffeine_mg))
    pruned: list[KnapsackItem] = []
    for item in usable:
        better = any(
            other.cost_cents <= item.cost_cents and other.caffeine_mg >= item.caffeine_mg for other in pruned
        )
        if not better:
            pruned = [
                other
                for other in pruned
                if not (other.cost_cents >= item.cost_cents and other.caffeine_mg <= item.caffeine_mg)
            ]
            pruned.append(item)
    usable = pruned

    best = [0.0] * (budget_cents + 1)
    pick: list[int] = [-1] * (budget_cents + 1)
    for capacity in range(1, budget_cents + 1):
        best[capacity] = best[capacity - 1]
        pick[capacity] = -1
        for index, item in enumerate(usable):
            if item.cost_cents <= capacity:
                candidate = best[capacity - item.cost_cents] + item.caffeine_mg
                if candidate > best[capacity]:
                    best[capacity] = candidate
                    pick[capacity] = index

    result = KnapsackResult(spent_cents=0, caffeine_mg=best[budget_cents])
    capacity = budget_cents
    counts: dict[int, int] = {}
    while capacity > 0:
        index = pick[capacity]
        if index < 0:
            capacity -= 1
            continue
        counts[index] = counts.get(index, 0) + 1
        capacity -= usable[index].cost_cents
    for index, count in counts.items():
        item = usable[index]
        result.items.append((item, count))
        result.spent_cents += item.cost_cents * count
    result.items.sort(key=lambda pair: -pair[0].caffeine_mg * pair[1])
    return result


def distance_travelled(points: list[tuple[float, float]], origin: tuple[float, float]) -> float:
    """Straight-line nearest-neighbour tour length in km, starting and ending at origin.

    This is crow flight only; cost a plan with drive_distance_km() instead, and
    keep this value if you need to show what the geometry alone said.
    """
    from .api.deps import haversine_km

    remaining = list(points)
    current = origin
    total = 0.0
    while remaining:
        nearest = min(remaining, key=lambda point: haversine_km(current[0], current[1], point[0], point[1]))
        total += haversine_km(current[0], current[1], nearest[0], nearest[1])
        current = nearest
        remaining.remove(nearest)
    if points:
        total += haversine_km(current[0], current[1], origin[0], origin[1])
    return total


def drive_distance_km(straight_km: float, drive_factor: float | None = None) -> float:
    """How far the car actually travels for a straight-line distance.

    Haversine draws a line across the Swan and through Perth's street grid; the
    driven route is ~30% longer. Without this factor a multi-store trip looks
    cheaper than the detour really is. Factors below 1 are ignored: a road can
    never beat the straight line it replaces.
    """
    factor = settings.drive_factor if drive_factor is None else drive_factor
    return straight_km * max(1.0, factor)


def fuel_cost_cents(travel_km: float, fuel_price_cents_per_l: int | None) -> int:
    """Fuel for a driven tour at the configured consumption, in whole cents."""
    if not fuel_price_cents_per_l:
        return 0
    return round(travel_km * settings.vehicle_l_per_100km / 100 * fuel_price_cents_per_l)

