-- Alerts can fire on "a special just started", not only on a price ceiling.
alter table price_alerts add column if not exists on_special boolean not null default false;

-- The app's watchlist lives on the phone; the weekly digest needs it server-side,
-- together with where that phone usually is.
create table if not exists watchlist (
    device_id integer primary key references devices(id) on delete cascade,
    slugs text[] not null default '{}',
    lat double precision,
    lon double precision,
    radius_km integer not null default 15,
    updated_at timestamptz not null default now()
);

-- Sparkline reads one product's daily history across the stores in an area.
create index if not exists ix_price_daily_store_product_day
    on price_daily (store_id, product_id, day desc);

-- The trigger check from 009 has to learn about the new trigger, or a
-- special-only alert fails the constraint on insert.
alter table price_alerts drop constraint if exists ck_alert_trigger;
alter table price_alerts add constraint ck_alert_trigger check (
    max_price_cents is not null or min_drop_percent is not null or on_special
);
