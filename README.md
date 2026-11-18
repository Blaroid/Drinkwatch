# DrinkWatch

Energy drink prices near you, in Perth, Western Australia.

The supermarket sites publish prices; nobody publishes petrol-station or IGA
prices, so those come from people using the app. This repo is the backend: the
scrapers, the price ledger, the crowd API and the plans (basket, budget, route).

* **API + docs:** https://cans.hazzardous.online/docs
* **Android app:** https://cans.hazzardous.online/app (sideload, ~15 MB)
* **App source:** https://github.com/Blaroid/Drinkwatch-Android

## What it does

* Keeps a directory of ~550 Perth stores (Woolworths, Coles, SpudShed, IGA,
  7-Eleven, Vibe, Ampol, BP) with coordinates, from OpenStreetMap.
* Scrapes the chains that publish prices, and stores one price ledger that both
  sources write into.
* Accepts **crowd sightings** from the app: price, photo of the tag, scanned
  barcode, and (for unknown cans) the name off the label.
* Serves small JSON for the phone widget.
* Answers the questions that need maths: *which store is cheapest for this list*,
  *what is the best use of $20 if I only care about caffeine*, *is the drive worth
  it once fuel is counted*.

## Data sources, honestly

| Source | Prices | How |
|---|---|---|
| SpudShed | real, per store | 7 online stores, each its own subdomain, plain HTML |
| Woolworths | real, per state | JSON search API behind an Akamai cookie checkpoint |
| Coles | real, per state | Next.js data route; Akamai bot-walls the IP after bursts, so it is best effort |
| IGA | none | independently owned; only some stores have online shops. Crowd only |
| 7-Eleven / Vibe / Ampol / BP | none published anywhere | crowd only, by design |

Every price row carries `price_scope`:

* `store` - the price belongs to that shop (SpudShed, crowd reports)
* `chain` - the chain's online price, attributed to every store of that chain at
  query time. The app labels these "online".

## Architecture

    scrapers (spudshed / woolworths / coles)        Android app (crowd)
            |                                              |
            v                                              v
      +----------------------------------------------------------+
      |  FastAPI  127.0.0.1:8788  (docker)                        |
      |    /v1/...            read API, crowd API, admin API      |
      |    APScheduler        spudshed 6h, ww 05:20, coles 05:45  |
      |                       alerts every 30 min                 |
      +----------------------------------------------------------+
            |                                    |
            v                                    v
      PostgreSQL 17                      cloudflared tunnel
      (docker volume)                    cans.hazzardous.online

Price maths lives in `app/pricing.py` and is pure: bundle extraction (single /
pack / multibuy), integer coin change for "cheapest way to buy N cans", and an
exact unbounded knapsack for "most caffeine for $X".

## Quick start

Requires Docker.

    cp .env.example .env          # set ADMIN_TOKEN
    docker compose up -d --build
    docker compose exec -T api python scripts/migrate.py
    docker compose exec -T api python scripts/seed.py            # chains + OSM stores
    docker compose exec -T api python scripts/seed_spudshed.py   # SpudShed subdomains
    docker compose exec -T api python scripts/geocode_stores.py  # suburb names
    docker compose exec -T api python -m app.scrape.runner --all

Then:

    curl -s localhost:8788/v1/meta
    curl -s "localhost:8788/v1/widget?lat=-31.9523&lon=115.8613&radius_km=15" | jq

## API in one screen

Full reference: [API.md](API.md). Interactive: `/docs`.

    GET  /v1/widget?lat&lon&radius_km&products&suggest    phone widget payload
    GET  /v1/prices?product&lat&lon&radius_km             every store's price + deal badge
    GET  /v1/products?q=monstr                            typo-tolerant search
    GET  /v1/stores/search?q=spud&lat&lon                 store picker
    GET  /v1/barcode/{code}                               EAN-13 / GTIN-14 / UPC-A / retailer codes
    POST /v1/products/pending                             unknown can -> review queue
    POST /v1/photos                                       price-tag photo (EXIF stripped)
    POST /v1/observations                                 crowd price (dedupe + outlier check)
    POST /v1/basket/compare                               price a shopping list per store
    GET  /v1/value/max-caffeine?budget_cents&mode         knapsack plan
    GET  /v1/plan/route?items=slug:qty,...                multi-store plan + FuelWatch fuel cost
    POST /v1/alerts                                       "tell me when it drops"
    GET  /v1/caffeine/today                               daily gauge
    POST /v1/admin/scrape/run                             trigger a scrape (X-Admin-Token)

## Project layout

    app/
      main.py              FastAPI app, lifespan, middleware
      config.py            env-driven settings
      db.py                engine, sessions, schema bootstrap
      models.py            SQLAlchemy models - the schema of record
      schemas.py           request/response models
      security.py          device tokens, admin token, rate limiting
      pricing.py           bundle / coin-change / knapsack maths (pure)
      notify.py            price alert evaluation -> notifications
      scheduler.py         APScheduler jobs
      api/                 public, crowd, admin, photos, barcode, basket, alerts, appdownload
      scrape/              spudshed, woolworths, coles, iga, fuelwatch, normalize, runner
    scripts/               seed, migrate, backfill, clean_names, photos-sync, smoke
    migrations/            SQL migrations, applied in filename order
    docs/                  screenshots

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | postgres in compose | Postgres DSN |
| `PUBLIC_BASE_URL` | http://127.0.0.1:8788 | links in API responses |
| `ADMIN_TOKEN` | - | required for `/v1/admin/*` |
| `SCRAPE_ENABLED` | true | master switch for scrapers |
| `SCRAPE_USER_AGENT` | DrinkWatch/0.1 | honest bot UA for seeding/geocoding |
| `SCRAPE_BROWSER_UA` | Chrome 131 | Woolworths/Coles entry pages |
| `SCHEDULER_ENABLED` | true | in-process APScheduler |
| `PHOTOS_DIR` / `PHOTOS_HOST_DIR` | /srv/photos, ./photos | photo cache |
| `OFF_LOOKUP_ENABLED` | true | Open Food Facts suggestion fallback |
| `FUELWATCH_ENABLED` | true | FuelWatch WA RSS for ULP prices |
| `VEHICLE_L_PER_100KM` | 8.5 | fuel estimate for route plans |
| `MAX_BUDGET_CENTS` | 20000 | cap for the budget optimiser |
| `DAILY_CAFFEINE_LIMIT_MG` | 400 | guideline used by the gauge |

## Operations

* **Migrations** - `docker compose exec -T api python scripts/migrate.py`. Applied
  files are tracked in `schema_migrations`.
* **Scrapes** - scheduled in-process; manual runs via
  `python -m app.scrape.runner --chain spudshed` (`--list` shows what is enabled).
* **Photos** - the api container writes `./photos` as uid 1000; a systemd user
  timer mirrors them to the redmax backup disk every 10 minutes and prunes local
  copies after 14 days (`scripts/photos-sync.sh`).
* **Backups** - data lives in the `drinkwatch_pgdata` volume:
  `docker compose exec -T db pg_dump -U drinkwatch drinkwatch | gzip > backup.sql.gz`
* **Smoke test** - `docker compose exec -T api python scripts/smoke.py http://127.0.0.1:8000`
  exercises 30 read/crowd/plan paths. It writes real rows; clean them with the SQL
  in the script header.

## Data model notes

* `products` is one row per brand + flavour + size + pack. Names are the flavour
  only, with "Original" for the plain variant; the app composes
  "Monster Ultra Zero 500ml x4" from brand + name + size.
* Retailer spellings of the same can are linked with `products.canonical_product_id`
  (curated: `/v1/admin/products/suggestions` proposes pairs, `/products/{slug}/link`
  confirms). Prices are merged across a group at query time.
* `barcodes` maps *every* code form (EAN-13, GTIN-14, UPC-A, retailer-internal
  codes) to a product, because Woolworths' 14-digit codes do not survive naive
  normalisation.
* `observations` is the append-only price ledger. `store_id` is NULL for
  chain-wide online prices. A `dedupe_key` makes scraper writes idempotent.
* `price_daily` keeps one row per (day, chain, product) so deal badges still have
  history when a price does not change for weeks.

## Versioning

`app/__init__.py` holds `__version__`, surfaced at `GET /v1/meta`. Every behavioural
change bumps it and lands as one commit:

    0.1.0  launch: scrapers, crowd API, widget
    0.1.1  clean product names ("Original" fallback)
    0.2.0  typo-tolerant product search (pg_trgm)
    0.3.1  /v1/widget?suggest for "end of your list" suggestions
    docs   README / API.md only, no version bump

## Legal and etiquette

Scraping Woolworths and Coles violates their terms of service. This runs at
roughly 20 requests a day, cached, rate-limited, for personal and friends-scale
use. Do not turn it into a commercial price feed.

Attribution required in any UI built on this: store locations from
**OpenStreetMap** (ODbL), geocoding from **Nominatim**, fuel prices from
**FuelWatch WA**.

## If this ever covers more than drinks

The model is built around canned drinks. These are the load-bearing assumptions,
and what would need to change for snacks, coffee or anything sold by weight.

1. `size_ml` + `pack_count` are the only size model - snacks need unit basis
   (grams/pieces) and a per-category unit price.
2. `caffeine_mg_per_100ml` should become a generic nutrient block.
3. `family_slug` (brand + flavour + size, `-xN` stripped) assumes drink naming.
4. `scrape/*` keyword lists and `is_energy_item` are energy-drink specific.
5. The budget optimiser is "max caffeine per dollar"; a generic version takes the
   nutrient as a parameter.
6. `price_daily` percentiles assume frequent repricing.
7. The widget's default catalogue is "8 most stocked energy drinks, max 2/brand".
8. Barcode-first onboarding assumes a GTIN; deli/produce needs a photo+name+price/kg path.
