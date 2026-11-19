"""Pydantic request/response models for the public and crowd APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    kind: str


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    suburb: str | None = None
    state: str | None = None
    postcode: str | None = None
    lat: float | None = None
    lon: float | None = None
    website: str | None = None
    chain: ChainOut
    distance_km: float | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    brand: str
    name: str
    variant: str | None = None
    size_ml: int | None = None
    pack_count: int
    barcode: str | None = None
    category: str


class DealStatsOut(BaseModel):
    median_90d_cents: int | None = None
    min_90d_cents: int | None = None
    deal: str = "unknown"  # great | good | typical | high | unknown


class OfferOut(BaseModel):
    """One store's current price for a product."""

    observation_id: int | None = None
    store_id: int
    store_name: str
    chain_code: str
    chain_name: str
    chain_kind: str
    suburb: str | None = None
    distance_km: float | None = None
    price_cents: int
    price: str
    was_price_cents: int | None = None
    per_can_cents: int | None = None
    per_litre_cents: int | None = None
    promo_kind: str
    promo_qty: int | None = None
    promo_total_cents: int | None = None
    in_stock: bool | None = None
    source: str
    price_scope: str
    age_hours: float
    observed_at: datetime
    stats: DealStatsOut | None = None


class ProductPricesOut(BaseModel):
    product: ProductOut
    cheapest: OfferOut | None = None
    offers: list[OfferOut]


class WidgetItemOut(BaseModel):
    """Compact row for an Android widget."""

    slug: str
    label: str
    price_cents: int
    price: str
    per_can_cents: int | None = None
    store: str
    chain: str
    suburb: str | None = None
    distance_km: float | None = None
    promo: str
    source: str
    age_hours: float
    suggested: bool = False


class WidgetOut(BaseModel):
    generated_at: datetime
    radius_km: float
    items: list[WidgetItemOut]


class DeviceRegisterIn(BaseModel):
    platform: Literal["android", "ios", "web", "other"] = "android"
    app_version: str | None = Field(default=None, max_length=20)
    display_name: str | None = Field(default=None, max_length=60)


class DeviceOut(BaseModel):
    id: int
    token: str
    display_name: str | None = None
    reputation: int
    created_at: datetime


class DeviceMeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str | None = None
    platform: str | None = None
    reputation: int
    is_banned: bool
    created_at: datetime
    last_seen_at: datetime | None = None


class ObservationIn(BaseModel):
    store_id: int
    product_slug: str | None = None
    barcode: str | None = None
    price_cents: int = Field(gt=0, le=50000)
    was_price_cents: int | None = Field(default=None, gt=0, le=50000)
    promo_kind: Literal["none", "special", "multibuy", "clearance"] = "none"
    promo_qty: int | None = Field(default=None, ge=2, le=50)
    promo_total_cents: int | None = Field(default=None, gt=0, le=200000)
    in_stock: bool | None = None
    note: str | None = Field(default=None, max_length=300)
    photo_url: str | None = Field(default=None, max_length=500)
    photo_token: str | None = Field(default=None, max_length=64)
    observed_at: datetime | None = None


class ObservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    product_id: int
    price_cents: int
    was_price_cents: int | None = None
    unit_price_cents_per_l: int | None = None
    promo_kind: str
    promo_qty: int | None = None
    promo_total_cents: int | None = None
    in_stock: bool | None = None
    source: str
    price_scope: str
    note: str | None = None
    # Evidence for the sighting. photo_token is the /v1/photos handle and
    # photo_url is the URL for the same image; legacy rows that stored only a
    # client-supplied photo_url carry that URL with no token.
    photo_token: str | None = None
    photo_url: str | None = None
    observed_at: datetime
    status: str
    confirms: int = 0
    disputes: int = 0
    # Repeat reports of the same price at the same store within the hour are
    # folded into one entry; this is how many sightings that entry stands for.
    sightings: int = 1


class VoteIn(BaseModel):
    vote: Literal[-1, 1]


class ReportIn(BaseModel):
    observation_id: int | None = None
    reason: Literal["wrong_price", "wrong_product", "wrong_store", "closed", "abuse", "other"]
    note: str | None = Field(default=None, max_length=500)


class PhotoOut(BaseModel):
    id: int
    token: str
    url: str
    thumb_url: str | None = None
    content_type: str
    bytes: int
    width: int | None = None
    height: int | None = None
    created_at: datetime


class BarcodeLookupOut(BaseModel):
    found: bool
    barcode: str
    normalised: str | None = None
    checksum_ok: bool = False
    product: ProductOut | None = None
    suggestion: dict | None = None
    pending_hint: str | None = None


class PendingProductIn(BaseModel):
    barcode: str = Field(min_length=6, max_length=20)
    name: str = Field(min_length=2, max_length=160)
    brand: str | None = Field(default=None, max_length=60)
    size_ml: int | None = Field(default=None, ge=50, le=5000)
    pack_count: int = Field(default=1, ge=1, le=48)
    category: str = Field(default="energy", max_length=40)
    photo_token: str | None = Field(default=None, max_length=64)
    store_id: int | None = None
    price_cents: int | None = Field(default=None, gt=0, le=50000)
    note: str | None = Field(default=None, max_length=300)


class PendingProductOut(BaseModel):
    product: ProductOut
    created: bool
    observation_id: int | None = None


class BasketItemIn(BaseModel):
    slug: str = Field(description="product slug or family slug")
    quantity: int = Field(ge=1, le=48)


class BasketCompareIn(BaseModel):
    items: list[BasketItemIn] = Field(min_length=1, max_length=12)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float | None = Field(default=None, gt=0)
    max_age_hours: int | None = Field(default=None, ge=1, le=720)
    include_fuel: bool = Field(
        default=True,
        description=(
            "rank plans by items + fuel; false reproduces the old price-only ranking, "
            "with fuel still reported per store"
        ),
    )


class BasketLineOut(BaseModel):
    requested: str
    quantity: int
    product_slug: str | None = None
    label: str | None = None
    units: int = 0
    cost_cents: int = 0
    price: str = ""
    breakdown: list[str] = []


class BasketStoreOut(BaseModel):
    store_id: int
    store_name: str
    chain_code: str
    suburb: str | None = None
    distance_km: float = Field(description="straight-line km from origin")
    drive_distance_km: float = Field(
        default=0.0, description="round-trip road km after DRIVE_FACTOR, the values fuel uses"
    )
    fuel_cost_cents: int = Field(default=0, description="fuel for that round trip")
    total_cents: int = Field(description="item cost only")
    total_with_fuel_cents: int = Field(default=0, description="total_cents + fuel_cost_cents")
    total: str
    total_with_fuel: str = ""
    complete: bool
    items: list[BasketLineOut]


class BasketCompareOut(BaseModel):
    generated_at: datetime
    radius_km: float
    stores: list[BasketStoreOut]
    best_single_store: BasketStoreOut | None = None
    include_fuel: bool = True
    drive_factor: float = Field(default=1.0, description="road distance = straight line x this")
    fuel_price_cents_per_l: int | None = Field(
        default=None, description="ULP price used for the multi-store plan"
    )
    fuel_source: str | None = Field(default=None, description="fuelwatch or fallback")
    multi_store_items_cents: int | None = None
    multi_store_straight_line_km: float | None = Field(
        default=None, description="crow-flight tour length, before DRIVE_FACTOR"
    )
    multi_store_travel_km: float | None = Field(default=None, description="driven tour length")
    multi_store_fuel_cost_cents: int | None = None
    multi_store_total_cents: int | None = Field(
        default=None, description="items (+ fuel unless include_fuel is false) for the multi-store plan"
    )
    multi_store_plan: list[dict] = []
    savings_vs_worst_cents: int | None = None


class BudgetItemOut(BaseModel):
    slug: str
    label: str
    store_id: int
    store_name: str
    chain_code: str
    units: int
    cost_cents: int
    cost: str
    caffeine_mg: int


class MaxCaffeineOut(BaseModel):
    budget_cents: int
    mode: str
    spent_cents: int
    leftover_cents: int
    total_caffeine_mg: int
    total_ml: int
    items: list[BudgetItemOut]
    note: str
    daily_limit_mg: int
    exceeds_daily_limit: bool
    stores_used: int


class RouteStopOut(BaseModel):
    store_id: int
    store_name: str
    chain_code: str
    suburb: str | None = None
    lat: float | None = None
    lon: float | None = None
    subtotal_cents: int
    subtotal: str
    items: list[dict] = []
    distance_from_origin_km: float | None = None


class RoutePlanOut(BaseModel):
    generated_at: datetime
    origin: dict
    stops: list[RouteStopOut]
    items_total_cents: int
    straight_line_km: float | None = Field(default=None, description="crow-flight tour, before DRIVE_FACTOR")
    travel_km: float = Field(description="driven tour length, the one fuel is charged on")
    drive_factor: float = Field(default=1.0, description="road distance = straight line x this")
    fuel_price_cents_per_l: int | None = None
    fuel_cost_cents: int
    total_cost_cents: int
    single_store_baseline_cents: int | None = None
    net_saving_cents: int | None = None
    fuel_source: str | None = None


class AlertIn(BaseModel):
    product_slug: str | None = None
    family_slug: str | None = None
    # At least one trigger is required: an absolute ceiling and/or a relative drop
    # below the product's 30-day median. One may now be omitted; old clients that
    # always send max_price_cents behave exactly as before.
    max_price_cents: int | None = Field(default=None, gt=0, le=50000)
    min_drop_percent: int | None = Field(default=None, ge=1, le=90)
    on_special: bool = False
    chain_code: str | None = Field(default=None, max_length=32)
    store_id: int | None = None
    # Location filter for "alert me about shops near me"; all three or none.
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(default=None, gt=0)


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int | None = None
    family_slug: str | None = None
    chain_code: str | None = None
    store_id: int | None = None
    max_price_cents: int | None = None
    min_drop_percent: int | None = None
    lat: float | None = None
    lon: float | None = None
    radius_km: float | None = None
    is_active: bool
    last_notified_at: datetime | None = None
    created_at: datetime


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    title: str
    body: str
    payload: dict = {}
    read_at: datetime | None = None
    created_at: datetime


class CaffeineIn(BaseModel):
    product_slug: str | None = None
    label: str | None = Field(default=None, max_length=160)
    qty: int = Field(default=1, ge=1, le=24)
    mg_caffeine: int | None = Field(default=None, ge=0, le=5000)
    consumed_at: datetime | None = None


class CaffeineEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int | None = None
    label: str | None = None
    qty: int
    mg_caffeine: int
    consumed_at: datetime


class CaffeineTodayOut(BaseModel):
    date: str
    total_mg: int
    limit_mg: int
    remaining_mg: int
    entries: list[CaffeineEntryOut]


class BatchObservationIn(BaseModel):
    items: list[ObservationIn] = Field(min_length=1, max_length=50)


class BatchObservationResult(BaseModel):
    index: int
    status: str
    observation_id: int | None = None
    error: str | None = None


class ReceiptParseIn(BaseModel):
    text: str = Field(min_length=3, max_length=20000)


class ReceiptAlternative(BaseModel):
    """Another catalogue row that matched a receipt line equally well."""

    product_slug: str
    label: str
    score: float


class ReceiptCandidate(BaseModel):
    line: str
    price_cents: int | None = None
    product_slug: str | None = None
    label: str | None = None
    score: float = 0.0
    # Populated only when the line's size/pack tokens still leave more than one
    # row with the same best score, so the user can pick instead of the
    # database order deciding.
    alternatives: list[ReceiptAlternative] = Field(default_factory=list)


class LeaderboardEntry(BaseModel):
    rank: int
    device: str
    reputation: int
    accepted: int


class MetaOut(BaseModel):
    name: str
    version: str
    time: datetime
    chains: list[ChainOut]
    store_count: int
    product_count: int
    observation_count: int
    freshest_observation_at: datetime | None = None
    currency: str = "AUD"


class HistoryPoint(BaseModel):
    day: str
    price_cents: int
    store_name: str | None = None


class HistoryOut(BaseModel):
    product: ProductOut
    days: int
    points: list[HistoryPoint]
    median_cents: int | None = None
    min_cents: int | None = None
    max_cents: int | None = None
    stores_seen: int = 0


class DealOut(BaseModel):
    product: ProductOut
    store_id: int
    store_name: str
    chain_code: str
    suburb: str | None = None
    distance_km: float | None = None
    price_cents: int
    price: str
    per_can_cents: int | None = None
    per_litre_cents: int | None = None
    promo_kind: str
    age_hours: float


class CatalogueProduct(BaseModel):
    slug: str
    brand: str
    name: str
    size_ml: int | None = None
    pack_count: int = 1
    barcode: str | None = None


class CatalogueOut(BaseModel):
    generated_at: datetime
    products: list[CatalogueProduct]
    barcodes: dict[str, str]


class ConfirmIn(BaseModel):
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class WatchlistIn(BaseModel):
    slugs: list[str] = Field(default_factory=list, max_length=40)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_km: int = Field(default=15, ge=1, le=100)


class WatchlistOut(BaseModel):
    slugs: list[str]
    lat: float | None = None
    lon: float | None = None
    radius_km: int
    updated_at: datetime | None = None
