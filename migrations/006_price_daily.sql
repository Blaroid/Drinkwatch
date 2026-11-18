-- Daily price snapshots, for "is this a good deal?" history.
--
-- observations dedupe identical sightings, which is right for storage but useless
-- for history: an unchanged price writes no row. price_daily keeps exactly one
-- row per (day, chain, product) so percentiles stay cheap and complete.

create table if not exists price_daily (
    day date not null,
    chain_id integer not null references chains(id) on delete cascade,
    product_id integer not null references products(id) on delete cascade,
    price_cents integer not null,
    was_price_cents integer,
    promo_kind text not null default 'none',
    source text not null default 'scrape',
    primary key (day, chain_id, product_id)
);
create index if not exists ix_price_daily_product on price_daily (product_id, day desc);

insert into price_daily (day, chain_id, product_id, price_cents, was_price_cents, promo_kind, source)
select (o.observed_at at time zone 'Australia/Perth')::date,
       o.chain_id, o.product_id, min(o.price_cents), max(o.was_price_cents),
       min(o.promo_kind), min(o.source)
  from observations o
 where o.status = 'accepted'
 group by 1, 2, 3
on conflict (day, chain_id, product_id) do nothing;
