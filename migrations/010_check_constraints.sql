-- Close the enum-ish text columns.
--
-- source, price_scope, status and promo_kind are written from a fixed set of
-- strings in app/, but nothing at the database level enforced that set. A typo
-- was silent: a crowd report saved with price_scope='shop' disappears from every
-- query, and price_scope='store' with store_id NULL claims a store price without
-- saying which store. The two shapes the read paths in app/api/deps.py rely on
-- are therefore both enforced here: a store row names its store, a chain row
-- names its chain and no store (otherwise the same sighting is counted twice --
-- once per store through store_id, once chain-wide).
--
-- A violating legacy row makes the ADD CONSTRAINT below fail and the whole file
-- roll back. That is intended: this migration reports bad data through the
-- server error and the operator fixes it, rather than the migration guessing or
-- deleting rows. Nothing here rewrites or removes a row.
--
-- Constraints are dropped first so the file is also correct on a fresh database,
-- where app.db.init_db() has already built these tables from app/models.py with
-- the same constraint names.
--
-- Deliberately not constrained:
--   * price_daily: derived rows written by aggregation SQL (min() per day over
--     observations), so its columns inherit validity from observations rather
--     than from one writer with a known set of values.
--   * barcodes.source and barcode_cache.source: provenance tags that gain a new
--     value with each importer or external lookup; they are open by design.

alter table observations
    drop constraint if exists ck_obs_source,
    drop constraint if exists ck_obs_price_scope,
    drop constraint if exists ck_obs_status,
    drop constraint if exists ck_obs_promo_kind,
    drop constraint if exists ck_obs_scope_location;

alter table observations
    add constraint ck_obs_source
        check (source in ('scrape', 'crowd')),
    add constraint ck_obs_price_scope
        check (price_scope in ('store', 'chain')),
    add constraint ck_obs_status
        check (status in ('accepted', 'needs_review', 'rejected')),
    add constraint ck_obs_promo_kind
        check (promo_kind in ('none', 'special', 'multibuy', 'clearance')),
    add constraint ck_obs_scope_location
        check (
            (price_scope = 'store' and store_id is not null)
            or (price_scope = 'chain' and store_id is null and chain_id is not null)
        );

alter table products
    drop constraint if exists ck_products_status,
    drop constraint if exists ck_products_source;

alter table products
    add constraint ck_products_status
        check (status in ('verified', 'pending', 'rejected')),
    -- 'crowd' is set when the barcode endpoint creates a product from a photo.
    add constraint ck_products_source
        check (source in ('auto', 'manual', 'crowd'));

alter table scrape_runs
    drop constraint if exists ck_scrape_runs_status;

alter table scrape_runs
    add constraint ck_scrape_runs_status
        check (status in ('running', 'ok', 'error'));
