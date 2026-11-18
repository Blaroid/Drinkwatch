-- Barcode index.
--
-- A single product shows up under several code forms: the can's EAN-13, a
-- retailer GTIN-14 with a packaging indicator, UPC-A with a leading zero, and
-- retailer-internal codes that are not valid GTINs at all. Rather than forcing
-- one canonical form (which loses information when the retailer's code has a
-- different check digit), every observed form maps to a product here.

create table if not exists barcodes (
    barcode text primary key,
    product_id integer not null references products(id) on delete cascade,
    source text not null default 'scrape',
    created_at timestamptz not null default now()
);
create index if not exists ix_barcodes_product on barcodes (product_id);
