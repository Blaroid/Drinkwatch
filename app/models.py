"""SQLAlchemy models: stores, catalogue, price observations, crowd accounts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Chain(Base):
    """A retailer brand: woolworths, coles, spudshed, iga, 7eleven, vibe, bp, ampol ..."""

    __tablename__ = "chains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    # supermarket | convenience | servo
    kind: Mapped[str] = mapped_column(String(24), default="supermarket")
    scrape_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (
        UniqueConstraint("chain_id", "external_ref", name="uq_stores_chain_extref"),
        Index("ix_stores_lat_lon", "lat", "lon"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(160))
    suburb: Mapped[str | None] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(8))
    postcode: Mapped[str | None] = mapped_column(String(8))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    website: Mapped[str | None] = mapped_column(String(300))
    # per-chain identifier: woolies StoreNo, spudshed subdomain, osm node id, coles id
    external_ref: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chain: Mapped[Chain] = relationship(lazy="joined")


class Product(Base):
    """A canonical drink: one row per brand+flavour+size+pack."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    brand: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(160))
    variant: Mapped[str | None] = mapped_column(String(80))
    size_ml: Mapped[int | None] = mapped_column(Integer)  # per can/bottle
    pack_count: Mapped[int] = mapped_column(Integer, default=1)
    barcode: Mapped[str | None] = mapped_column(String(32), unique=True)
    # set when this row is a retailer-specific spelling of another product row
    canonical_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL")
    )
    category: Mapped[str] = mapped_column(String(40), default="energy")
    # auto | manual
    source: Mapped[str] = mapped_column(String(16), default="auto")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Listing(Base):
    """A chain-specific SKU/page that maps to a canonical product."""

    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("chain_id", "external_sku", name="uq_listings_chain_sku"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id", ondelete="CASCADE"))
    external_sku: Mapped[str] = mapped_column(String(120))
    url: Mapped[str | None] = mapped_column(String(500))
    title_raw: Mapped[str | None] = mapped_column(String(300))
    size_text: Mapped[str | None] = mapped_column(String(60))
    pack_count: Mapped[int] = mapped_column(Integer, default=1)
    barcode: Mapped[str | None] = mapped_column(String(32))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Device(Base):
    """A crowd contributor: one anonymous device token per app install."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(60))
    platform: Mapped[str | None] = mapped_column(String(20))
    app_version: Mapped[str | None] = mapped_column(String(20))
    reputation: Mapped[int] = mapped_column(Integer, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Observation(Base):
    """A price sighting for one product at one store."""

    __tablename__ = "observations"
    __table_args__ = (
        Index("ix_obs_store_product_time", "store_id", "product_id", "observed_at"),
        Index("ix_obs_chain_product_time", "chain_id", "product_id", "observed_at"),
        Index("ix_obs_product_time", "product_id", "observed_at"),
        UniqueConstraint("dedupe_key", name="uq_obs_dedupe"),
        CheckConstraint("price_cents > 0", name="ck_obs_price_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # store_id is NULL for chain-wide online prices (Woolworths/Coles publish one
    # price for a whole state); those rows are attributed to stores at query time.
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id", ondelete="SET NULL"))
    price_cents: Mapped[int] = mapped_column(Integer)
    was_price_cents: Mapped[int | None] = mapped_column(Integer)
    unit_price_cents_per_l: Mapped[int | None] = mapped_column(Integer)
    # none | special | multibuy | clearance
    promo_kind: Mapped[str] = mapped_column(String(16), default="none")
    promo_qty: Mapped[int | None] = mapped_column(Integer)
    promo_total_cents: Mapped[int | None] = mapped_column(Integer)
    in_stock: Mapped[bool | None] = mapped_column(Boolean)
    # scrape | crowd
    source: Mapped[str] = mapped_column(String(10))
    # store = price specific to this store, chain = chain-wide online price
    price_scope: Mapped[str] = mapped_column(String(10), default="store")
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(String(300))
    photo_url: Mapped[str | None] = mapped_column(String(500))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # accepted | needs_review | rejected
    status: Mapped[str] = mapped_column(String(16), default="accepted")
    dedupe_key: Mapped[str | None] = mapped_column(String(160))


class ObservationVote(Base):
    __tablename__ = "observation_votes"

    observation_id: Mapped[int] = mapped_column(
        ForeignKey("observations.id", ondelete="CASCADE"), primary_key=True
    )
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    vote: Mapped[int] = mapped_column(SmallInteger)  # +1 confirm, -1 wrong
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("observations.id", ondelete="CASCADE")
    )
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain_code: Mapped[str] = mapped_column(String(32))
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    observations_written: Mapped[int] = mapped_column(Integer, default=0)
    products_created: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
