-- Feature pack: barcode/pending products, photos, alerts, caffeine log, notifications.

-- ---------- products ----------
alter table products add column if not exists family_slug text;
alter table products add column if not exists caffeine_mg_per_100ml numeric(5,2) not null default 32;
alter table products add column if not exists status text not null default 'verified';
alter table products add column if not exists image_url text;
alter table products add column if not exists normalised_barcode text;

-- family = the same drink ignoring pack size: monster-ultra-500ml-x4 -> monster-ultra-500ml
update products
   set family_slug = regexp_replace(slug, '-x[0-9]+$', '')
 where family_slug is null;

create index if not exists ix_products_family on products (family_slug);
create index if not exists ix_products_status on products (status);
create index if not exists ix_products_norm_barcode on products (normalised_barcode);

-- ---------- photos ----------
create table if not exists photos (
    id bigserial primary key,
    token text unique not null,
    filename text not null,
    content_type text not null default 'image/jpeg',
    bytes integer not null default 0,
    sha256 text not null,
    width integer,
    height integer,
    uploaded_by integer references devices(id) on delete set null,
    created_at timestamptz not null default now()
);
create index if not exists ix_photos_sha on photos (sha256);

alter table observations add column if not exists photo_id bigint references photos(id) on delete set null;
alter table observations add column if not exists ocr_text text;

-- ---------- barcode lookups (external fallback cache) ----------
create table if not exists barcode_cache (
    barcode text primary key,
    source text not null,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

-- ---------- price alerts ----------
create table if not exists price_alerts (
    id serial primary key,
    device_id integer not null references devices(id) on delete cascade,
    product_id integer references products(id) on delete cascade,
    family_slug text,
    chain_code text,
    store_id integer references stores(id) on delete cascade,
    max_price_cents integer not null check (max_price_cents > 0),
    is_active boolean not null default true,
    last_notified_at timestamptz,
    created_at timestamptz not null default now(),
    constraint ck_alert_target check (product_id is not null or family_slug is not null)
);
create index if not exists ix_alerts_active on price_alerts (is_active);
create index if not exists ix_alerts_device on price_alerts (device_id);

create table if not exists notifications (
    id bigserial primary key,
    device_id integer not null references devices(id) on delete cascade,
    kind text not null default 'alert',
    title text not null,
    body text not null,
    payload jsonb not null default '{}'::jsonb,
    read_at timestamptz,
    created_at timestamptz not null default now()
);
create index if not exists ix_notifications_device on notifications (device_id, created_at desc);

-- ---------- caffeine log ----------
create table if not exists caffeine_entries (
    id bigserial primary key,
    device_id integer not null references devices(id) on delete cascade,
    product_id integer references products(id) on delete set null,
    label text,
    qty integer not null default 1 check (qty > 0 and qty <= 24),
    mg_caffeine integer not null check (mg_caffeine >= 0 and mg_caffeine <= 5000),
    consumed_at timestamptz not null default now(),
    created_at timestamptz not null default now()
);
create index if not exists ix_caffeine_device_day on caffeine_entries (device_id, consumed_at desc);
