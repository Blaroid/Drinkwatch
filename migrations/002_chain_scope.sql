-- Chain-scoped price observations.
--
-- Woolworths and Coles publish one price per state, not per store. Writing that
-- price against 86 store rows created ~12k rows/day of invented precision.
-- Instead: store_id IS NULL + price_scope='chain', attributed to stores at query
-- time by the API.

alter table observations add column if not exists chain_id integer
    references chains(id) on delete cascade;

update observations o
   set chain_id = s.chain_id
  from stores s
 where o.store_id = s.id
   and o.chain_id is null;

delete from observations where chain_id is null;

alter table observations alter column chain_id set not null;
alter table observations alter column store_id drop not null;

create index if not exists ix_obs_chain_product_time
    on observations (chain_id, product_id, observed_at);

-- One-off cleanup: drop the store-expanded copies of chain-wide prices.
--
-- Only rows that still name a store are copies. A replay of this file (for
-- example after restoring data into a database whose schema_migrations was
-- empty) must leave the real chain-scope rows alone: they carry store_id NULL
-- and are the price history, not the duplicate this migration removed.
delete from observations where price_scope = 'chain' and store_id is not null;
