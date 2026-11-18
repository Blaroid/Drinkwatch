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
