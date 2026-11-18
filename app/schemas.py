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
    observed_at: datetime
    status: str
    confirms: int = 0
    disputes: int = 0


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
    include_fuel: bool = False


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
    distance_km: float
    total_cents: int
    total: str
    complete: bool
    items: list[BasketLineOut]


class BasketCompareOut(BaseModel):
    generated_at: datetime
    radius_km: float
    stores: list[BasketStoreOut]
    best_single_store: BasketStoreOut | None = None
    multi_store_total_cents: int | None = None
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
    travel_km: float
    fuel_price_cents_per_l: int | None = None
    fuel_cost_cents: int
    total_cost_cents: int
    single_store_baseline_cents: int | None = None
    net_saving_cents: int | None = None
    fuel_source: str | None = None


class AlertIn(BaseModel):
    product_slug: str | None = None
    family_slug: str | None = None
    max_price_cents: int = Field(gt=0, le=50000)
    chain_code: str | None = Field(default=None, max_length=32)
    store_id: int | None = None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int | None = None
    family_slug: str | None = None
    chain_code: str | None = None
    store_id: int | None = None
    max_price_cents: int
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


class ReceiptCandidate(BaseModel):
    line: str
    price_cents: int | None = None
    product_slug: str | None = None
    label: str | None = None
    score: float = 0.0


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
