-- Curated caffeine content, and where each value came from.
--
-- products.caffeine_mg_per_100ml was only ever written by the admin approve
-- endpoint, so every scraped or crowd-submitted can carried the FSANZ 2.6.4
-- fallback of 32 mg/100 mL. The cap is real, but it is a ceiling, not a fact
-- about a particular SKU (an imported Java can is stronger; a V can is
-- slightly weaker). This table holds values read off printed labels, keyed by
-- brand and a normalised flavour token set. An empty flavour is the brand-wide
-- default, applied only when no more specific row matches.
--
-- A table rather than a constant in code so a corrected label is a one-row
-- update, and so `source` can carry the provenance next to the number.

create table if not exists caffeine_reference (
    brand text not null,
    flavour text not null default '',
    caffeine_mg_per_100ml numeric(5, 2) not null,
    source text not null,
    created_at timestamptz not null default now(),
    primary key (brand, flavour),
    constraint ck_caffeine_reference_range
        check (caffeine_mg_per_100ml >= 0 and caffeine_mg_per_100ml <= 200)
);

-- Provenance on the product itself. Before this column every row looked the
-- same, so a backfill could not tell a curated 32 from an untouched default.
alter table products add column if not exists caffeine_source text not null default 'default';

-- Anything already off the default was entered by an admin; keep it and label
-- it as such so the seed backfill does not overwrite hand-checked values.
update products
   set caffeine_source = 'admin'
 where caffeine_mg_per_100ml is distinct from 32
   and caffeine_source = 'default';

-- Values are per 100 mL, derived from the caffeine figure on the can:
--   Red Bull / Coca-Cola Energy   80 mg per 250 mL  -> 32.0
--   Monster / Mother / Rockstar / Relentless
--                                160 mg per 500 mL  -> 32.0
--   V Energy                      78 mg per 250 mL  -> 31.2 (verify: see REPORT)
--   Monster Java (US import)     188 mg per 444 mL  -> 42.34
-- The brand-default rows exist because most variants of one brand share a base
-- formula; explicit flavour rows pin the SKUs actually checked.
insert into caffeine_reference (brand, flavour, caffeine_mg_per_100ml, source) values
    -- Red Bull: every AU variant uses the same caffeinated base.
    ('Red Bull', '',                       32.00, 'AU can label: 80 mg / 250 mL'),
    ('Red Bull', 'watermelon',             32.00, 'AU can label: 80 mg / 250 mL'),
    ('Red Bull', 'blue',                   32.00, 'AU can label: 80 mg / 250 mL'),
    ('Red Bull', 'tropical',               32.00, 'AU can label: 80 mg / 250 mL'),
    ('Red Bull', 'peach',                  32.00, 'AU can label: 80 mg / 250 mL'),
    ('Red Bull', 'coconut',                32.00, 'AU can label: 80 mg / 250 mL'),
    ('Red Bull', 'amber',                  32.00, 'AU can label: 80 mg / 250 mL'),

    -- Monster: 500 mL AU cans print 160 mg; no brand default because the
    -- imported Java line is deliberately different.
    ('Monster', 'original',                32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'lo carb',                 32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'absolutely zero',         32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'ultra',                   32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'ultra paradise',          32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'ultra watermelon',        32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'ultra rosa',              32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'ultra fiesta',            32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'mango loco',              32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'pipeline punch',          32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'khaos',                   32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'ripper',                  32.00, 'AU can label: 160 mg / 500 mL'),
    ('Monster', 'nitro super dry',         32.00, 'AU can label: 160 mg / 500 mL'),
    -- Java is an import with a US label; it is the reason a plain 32 default
    -- cannot answer "Ultra vs Java for the same money".
    ('Monster', 'java mean bean',          42.34, 'US import label: 188 mg / 444 mL'),
    ('Monster', 'java loca moca',          42.34, 'US import label: 188 mg / 444 mL'),
    -- Retailer titles often drop the "Java" line name; same can, same value.
    ('Monster', 'mean bean',               42.34, 'US import label: 188 mg / 444 mL'),
    ('Monster', 'loca moca',               42.34, 'US import label: 188 mg / 444 mL'),

    -- Mother: 500 mL range shares one formula.
    ('Mother', '',                          32.00, 'AU can label: 160 mg / 500 mL'),
    ('Mother', 'original',                  32.00, 'AU can label: 160 mg / 500 mL'),

    -- V: 250 mL cans print 78 mg, just under the cap. One brand row covers the
    -- 250 mL and 500 mL sizes because the value is per 100 mL.
    ('V', '',                               31.20, 'AU can label: 78 mg / 250 mL (verify)'),

    -- Rockstar / Relentless / Coca-Cola Energy: 32 mg/100 mL range.
    ('Rockstar', '',                        32.00, 'AU can label: 160 mg / 500 mL'),
    ('Rockstar', 'original',                32.00, 'AU can label: 160 mg / 500 mL'),
    ('Rockstar', 'punched',                 32.00, 'AU can label: 160 mg / 500 mL'),
    ('Rockstar', 'supersours',              32.00, 'AU can label: 160 mg / 500 mL'),
    ('Relentless', '',                      32.00, 'AU can label: 160 mg / 500 mL'),
    ('Relentless', 'origin',                32.00, 'AU can label: 160 mg / 500 mL'),
    ('Coca-Cola Energy', '',                32.00, 'AU can label: 80 mg / 250 mL'),
    ('Coca-Cola Energy', 'original',        32.00, 'AU can label: 80 mg / 250 mL')
on conflict (brand, flavour) do nothing;
