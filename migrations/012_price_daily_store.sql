-- Keep the store a daily price was observed at.
--
-- price_daily used to key on (day, chain, product). SpudShed publishes a price
-- per store (seven of them) and every crowd sighting is at a store, so several
-- shops wrote the same row and the last writer of the day won: SpudShed's daily
-- history was one arbitrary shop, and the 90-day deal median compared a single
-- store's price against a blend of different shops.
--
-- Now: one row per store, plus a chain-level aggregate row (store_id IS NULL)
-- for chain-wide charts and for chains that publish one price state-wide.
--
-- Postgres cannot put a nullable column in a primary key, so the old primary key
-- is replaced by a NULLS NOT DISTINCT unique index: NULL counts as a value there,
-- which makes the per-day chain row unique while leaving it nullable.

alter table price_daily add column if not exists store_id integer
    references stores(id) on delete cascade;

alter table price_daily drop constraint if exists price_daily_pkey;

create unique index if not exists uq_price_daily_day_chain_product_store
    on price_daily (day, chain_id, product_id, store_id) nulls not distinct;

-- Per-store price charts read one product at one store over time.
create index if not exists ix_price_daily_store
    on price_daily (store_id, product_id, day desc);

-- Backfill. Rows written before this migration were grouped per chain, so they
-- are already the chain-level row (store_id stays NULL) and stay in place for
-- chains that never had per-store observations.

-- 1. Recover the per-store history that observations still hold.
insert into price_daily
    (day, chain_id, product_id, store_id, price_cents, was_price_cents, promo_kind, source)
select (o.observed_at at time zone 'Australia/Perth')::date,
       o.chain_id,
       o.product_id,
       o.store_id,
       min(o.price_cents),
       max(o.was_price_cents),
       min(o.promo_kind),
       min(o.source)
  from observations o
 where o.status = 'accepted'
   and o.store_id is not null
 group by 1, 2, 3, 4
on conflict (day, chain_id, product_id, store_id) do update
   set price_cents = excluded.price_cents,
       was_price_cents = excluded.was_price_cents,
       promo_kind = excluded.promo_kind,
       source = excluded.source;

-- 2. Rebuild every chain-level row from the observations, so chain rows no longer
--    depend on which store wrote last. A published chain-wide price (store_id is
--    NULL in observations) still wins; chains that only price per store get the
--    median store, which is the rule the writers apply from here on.
insert into price_daily
    (day, chain_id, product_id, store_id, price_cents, was_price_cents, promo_kind, source)
select (o.observed_at at time zone 'Australia/Perth')::date,
       o.chain_id,
       o.product_id,
       null,
       coalesce(
           min(o.price_cents) filter (where o.store_id is null),
           round(percentile_cont(0.5) within group (order by o.price_cents)
                 filter (where o.store_id is not null))::int
       ),
       max(o.was_price_cents),
       coalesce(min(o.promo_kind) filter (where o.store_id is null), 'none'),
       coalesce(min(o.source) filter (where o.store_id is null), 'aggregate')
  from observations o
 where o.status = 'accepted'
 group by 1, 2, 3
on conflict (day, chain_id, product_id, store_id) do update
   set price_cents = excluded.price_cents,
       was_price_cents = excluded.was_price_cents,
       promo_kind = excluded.promo_kind,
       source = excluded.source;

analyze price_daily;
