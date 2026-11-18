-- Typo-tolerant product search.
--
-- "monstr ultra" or "red bul" should still find the can. pg_trgm gives us
-- similarity() and word_similarity() with a GIN index, which is cheap enough to
-- run on every keystroke debounce from the app.

create extension if not exists pg_trgm;

create index if not exists ix_products_search_trgm
    on products using gin ((lower(brand || ' ' || name || ' ' || coalesce(variant, ''))) gin_trgm_ops);

create index if not exists ix_products_brand_trgm
    on products using gin (lower(brand) gin_trgm_ops);
