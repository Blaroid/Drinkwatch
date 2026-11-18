-- Cross-chain product identity.
--
-- The same can is named differently by each retailer ("Monster Energy Ultra
-- Cans 500mL x 4 pack" vs "Monster Ultra Zero 4 x 500ml"). Name matching cannot
-- resolve those safely, so instead of guessing we let a human link rows:
--   products.canonical_product_id -> the row that represents the group.
-- Prices for a group are merged by the API; nothing is deleted.

alter table products add column if not exists canonical_product_id integer
    references products(id) on delete set null;

create index if not exists ix_products_canonical on products (canonical_product_id);
