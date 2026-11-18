# DrinkWatch

Energy drink prices near you, from supermarket websites plus crowd sightings.
Backend for the Android app and its home-screen widget.

Public API: https://cans.hazzardous.online (docs at /docs, OpenAPI at /openapi.json)

## What it does

* Keeps a directory of Perth stores (Woolworths, Coles, SpudShed, IGA, 7-Eleven,
  Vibe, Ampol, BP) with coordinates.
* Scrapes the chains that publish prices: **SpudShed** per store (each store is
  its own subdomain with its own prices), **Woolworths** and **Coles** at chain
  level (they publish one online price per state, not per store).
* Accepts **crowd price sightings** from app users - this is the only way to get
  petrol-station and IGA prices, and it overrides scraped prices when fresher.
* Serves a tiny JSON payload shaped for an Android widget.

## Data reality check

| Source | Prices | Notes |
|---|---|---|
| SpudShed | real, per store | 7 online stores x 2 categories, plain HTML |
| Woolworths | real, per state | JSON API behind an Akamai cookie checkpoint |
| Coles | real, per state | Next.js data route; Akamai bot-walls the IP after bursts, so it is best effort |
| IGA | none | stores are independently owned; only some have online shops. Crowd only for now |
| 7-Eleven / Vibe / Ampol / BP | none published anywhere | crowd only, by design |

`price_scope` on every price tells you which it is: `store` means the price is
specific to that shop, `chain` means it is the chain's online price attributed to
every store of that chain.

## Architecture

    scrapers (spudshed / woolworths / coles)        crowd (Android app)
            |                                              |
            v                                              v
      +--------------------------------------------------------+
      |  FastAPI  (docker, 127.0.0.1:8788)                     |
      |    /v1/...  read API, crowd API, admin API             |
      |    APScheduler: spudshed every 6h, woolworths + coles  |
      |                 daily at 05:20 / 05:45 Perth           |
      +--------------------------------------------------------+
            |                              |
            v                              v
      PostgreSQL 17                 cloudflared tunnel
      (docker volume)               cans.hazzardous.online

## Layout

    app/
      main.py            FastAPI app, lifespan, middleware
      config.py          env-driven settings
      db.py              engine, sessions, schema bootstrap
      models.py          SQLAlchemy models (the schema of record)
      schemas.py         request/response models
      security.py        device tokens, admin token, rate limiting
      api/public.py      read endpoints (stores, products, prices, widget)
      api/crowd.py       device registration, sightings, votes, reports
      api/admin.py       scraping control, product curation, moderation
      scrape/base.py     polite HTTP clients (httpx + curl_cffi), idempotent writes
      scrape/normalize.py product name parsing and matching
      scrape/spudshed.py / woolworths.py / coles.py / iga.py
      scrape/runner.py   CLI runner + global scrape lock
      scheduler.py       APScheduler jobs
    scripts/
      seed.py            chains + Perth stores from OpenStreetMap
      seed_spudshed.py   SpudShed stores from their own store chooser
      geocode_stores.py  suburb names for stores that OSM left bare
      migrate.py         applies migrations/*.sql
      cleanup.py         purges scraper noise after rule changes
      smoke.py           end-to-end checks against a running API
    migrations/          SQL migrations, applied in filename order

## Running it

On powermax (already deployed):

    cd ~/drinkwatch
    docker compose up -d --build     # api + postgres
    docker compose logs -f api
    curl -s localhost:8788/v1/meta

First-time bootstrap:

    docker compose exec -T api python scripts/migrate.py
    docker compose exec -T api python scripts/seed.py            # chains + OSM stores
    docker compose exec -T api python scripts/seed_spudshed.py   # SpudShed subdomains
    docker compose exec -T api python scripts/geocode_stores.py  # suburb names
    docker compose exec -T api python -m app.scrape.runner --all

Ongoing:

    docker compose exec -T api python -m app.scrape.runner --chain spudshed
    docker compose exec -T api python -m app.scrape.runner --list

The scheduler inside the API container handles the daily runs, so manual runs are
only for testing.

## Configuration

.env (see .env.example):

    DATABASE_URL=postgresql+psycopg://drinkwatch:drinkwatch@db:5432/drinkwatch
    PUBLIC_BASE_URL=https://cans.hazzardous.online
    ADMIN_TOKEN=<secret>          # required for /v1/admin/*
    SCRAPE_ENABLED=true
    SCRAPE_USER_AGENT=DrinkWatch/0.1 (contact: ...)
    SCRAPE_BROWSER_UA=...         # used for Woolworths/Coles entry pages
    SCHEDULER_ENABLED=true
    CORS_ORIGINS=*
    DEFAULT_RADIUS_KM=15
    MAX_RADIUS_KM=100
    DEFAULT_MAX_AGE_HOURS=72

## Schema notes

* `products` is one row per brand + flavour + size + pack. Retailer spellings
  that we cannot safely merge are kept as separate rows and linked by hand with
  `products.canonical_product_id` (see /v1/admin/products/suggestions).
* `observations` is the price ledger: append-only, one row per sighting.
  `store_id` is NULL for chain-wide online prices.
* Prices are integers in cents everywhere (`price_cents`). Never floats.
* `dedupe_key` makes scraper writes idempotent per day and per price change.

## Ops

Tunnel: `systemctl --user status drinkwatch-tunnel` (config in
`~/.config/drinkwatch/tunnel.yml`, logs via `journalctl --user -u drinkwatch-tunnel`).

Backups: the data lives in the `drinkwatch_pgdata` docker volume.

    docker compose exec -T db pg_dump -U drinkwatch drinkwatch | gzip > backup.sql.gz

## Legal / etiquette

Scraping Woolworths and Coles violates their terms of service. This runs at
roughly 20 requests per day, cached in the database, with an honest rate limit,
for personal/friends-scale use. Do not turn this into a commercial price feed,
and keep the retail-derived data unattributed to those retailers in the app UI
(show "online price" for `price_scope=chain`).

Store locations come from OpenStreetMap (ODbL), geocoding from Nominatim -
both need attribution in the app.

## Next

* Android app (Kotlin + Compose) with a Glance widget calling /v1/widget.
* Barcode scanning for crowd submissions (/v1/products/barcode/{barcode}).
* Product link curation pass: /v1/admin/products/suggestions.
* IGA scraper once we know which Perth stores use igashop.
