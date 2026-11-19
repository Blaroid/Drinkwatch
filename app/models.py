"""SQLAlchemy models: stores, catalogue, price observations, crowd accounts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
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
    __table_args__ = (
        CheckConstraint(
            "status in ('verified', 'pending', 'rejected')", name="ck_products_status"
        ),
        CheckConstraint(
            "source in ('auto', 'manual', 'crowd')", name="ck_products_source"
        ),
    )

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
    # same drink ignoring pack size, e.g. monster-ultra-500ml for ...-500ml-x4
    family_slug: Mapped[str | None] = mapped_column(String(160))
    # AU formulated caffeinated beverages are capped at 32mg/100mL (FSANZ 2.6.4).
    # That cap is the fallback for an unknown SKU, not a claim about this one;
    # see caffeine_source for where the number actually came from.
    caffeine_mg_per_100ml: Mapped[float] = mapped_column(Numeric(5, 2), default=32)
    # default | seed | openfoodfacts | admin
    caffeine_source: Mapped[str] = mapped_column(String(16), default="default")
    # verified | pending | rejected
    status: Mapped[str] = mapped_column(String(16), default="verified")
    image_url: Mapped[str | None] = mapped_column(String(500))
    normalised_barcode: Mapped[str | None] = mapped_column(String(14))
    category: Mapped[str] = mapped_column(String(40), default="energy")
    # auto | manual | crowd
    source: Mapped[str] = mapped_column(String(16), default="auto")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CaffeineReference(Base):
    """Curated caffeine content by brand and flavour, taken from printed labels.

    ``flavour`` is a normalised token key ("ultra paradise"); an empty key is the
    brand-wide default. Kept as data rather than code so a corrected can label is
    a row update, not a deploy, and ``source`` records where the number came
    from. Matching ignores pack size: caffeine per 100 mL does not change with it.
    """

    __tablename__ = "caffeine_reference"

    brand: Mapped[str] = mapped_column(String(60), primary_key=True)
    flavour: Mapped[str] = mapped_column(String(80), primary_key=True, default="")
    caffeine_mg_per_100ml: Mapped[float] = mapped_column(Numeric(5, 2))
    source: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
        # The read paths assume exactly two shapes: a store row names its store,
        # a chain row carries only the chain. A chain row with store_id set would
        # be counted both per-store and chain-wide.
        CheckConstraint(
            "(price_scope = 'store' and store_id is not null)"
            " or (price_scope = 'chain' and store_id is null and chain_id is not null)",
            name="ck_obs_scope_location",
        ),
        CheckConstraint("source in ('scrape', 'crowd')", name="ck_obs_source"),
        CheckConstraint("price_scope in ('store', 'chain')", name="ck_obs_price_scope"),
        CheckConstraint(
            "status in ('accepted', 'needs_review', 'rejected')", name="ck_obs_status"
        ),
        CheckConstraint(
            "promo_kind in ('none', 'special', 'multibuy', 'clearance')",
            name="ck_obs_promo_kind",
        ),
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
    # state a chain-scope price applies to (Woolworths/Coles publish per state);
    # null means "not state-specific" and is matched to every store of the chain
    state: Mapped[str | None] = mapped_column(String(8))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(String(300))
    photo_url: Mapped[str | None] = mapped_column(String(500))
    photo_id: Mapped[int | None] = mapped_column(ForeignKey("photos.id", ondelete="SET NULL"))
    ocr_text: Mapped[str | None] = mapped_column(Text)
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
    __table_args__ = (
        CheckConstraint(
            "status in ('running', 'ok', 'error')", name="ck_scrape_runs_status"
        ),
    )

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


class Photo(Base):
    """An uploaded price-tag or shelf photo (EXIF-stripped, stored on disk)."""

    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(60), default="image/jpeg")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # set by scripts/photos-sync.sh when the local cache copy is deleted; the bytes then
    # live on the backup disk only and both photo endpoints answer 410 instead of 404
    pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BarcodeCache(Base):
    """Cached external barcode lookups (including negative results)."""

    __tablename__ = "barcode_cache"

    barcode: Mapped[str] = mapped_column(String(20), primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    family_slug: Mapped[str | None] = mapped_column(String(160))
    chain_code: Mapped[str | None] = mapped_column(String(32))
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    # At least one of the two triggers is set (ck_alert_trigger in 009):
    # an absolute ceiling, and/or a fall of min_drop_percent below the 30-day median.
    max_price_cents: Mapped[int | None] = mapped_column(Integer)
    min_drop_percent: Mapped[int | None] = mapped_column(SmallInteger)
    # Location filter, all three or none (ck_alert_location in 009). None means
    # "every active store", which is what alerts did before the columns existed.
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    radius_km: Mapped[float | None] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20), default="alert")
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaffeineEntry(Base):
    __tablename__ = "caffeine_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    label: Mapped[str | None] = mapped_column(String(160))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    mg_caffeine: Mapped[int] = mapped_column(Integer)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Barcode(Base):
    """A code form (EAN-13, GTIN-14, UPC-A, retailer code) mapping to a product."""

    __tablename__ = "barcodes"

    barcode: Mapped[str] = mapped_column(String(20), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(20), default="scrape")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PriceDaily(Base):
    """One row per (day, chain, product, store): the history behind deal badges.

    ``store_id`` is NULL for the chain-level row (and for chains that only publish
    a chain-wide price), so the real key is the NULLS NOT DISTINCT unique index
    migration 012 installs: Postgres cannot put a nullable column in a primary
    key. Declaring those columns primary_key here would make create_all build a
    table with a 3-column key that the migrations then drop, so the table has no
    primary key at all and ``__mapper_args__`` supplies the ORM identity instead
    (nothing loads PriceDaily objects).
    """

    __tablename__ = "price_daily"
    __table_args__ = (
        Index(
            "uq_price_daily_day_chain_product_store",
            "day",
            "chain_id",
            "product_id",
            "store_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    day: Mapped[datetime] = mapped_column(Date)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    price_cents: Mapped[int] = mapped_column(Integer)
    was_price_cents: Mapped[int | None] = mapped_column(Integer)
    promo_kind: Mapped[str] = mapped_column(String(16), default="none")
    source: Mapped[str] = mapped_column(String(10), default="scrape")

    __mapper_args__ = {"primary_key": [day, chain_id, product_id]}
