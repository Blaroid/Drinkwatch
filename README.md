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

## Android app

The companion app lives in a separate repo (`~/drinkwatch-android`, also on the
powermax build box): Kotlin + Compose, a Glance home-screen widget, barcode
scanning, on-device price-tag OCR, basket/budget planners and alert notifications.

    https://cans.hazzardous.online/app                 install page
    https://cans.hazzardous.online/app/drinkwatch.apk  the APK itself
    https://cans.hazzardous.online/app/version.json    versionCode/versionName/notes

Publishing a build: copy the release APK to `~/drinkwatch/apk/drinkwatch.apk`,
update `version.json`, and the API serves it (the `apk/` directory is bind-mounted
read-only at `/srv/apk`). The app is signed with `~/drinkwatch-android/keystore/
drinkwatch.jks` (backed up to redmax); keep that key or updates stop working.

## Next

* Product link curation pass: /v1/admin/products/suggestions.
* Wear OS tile + complication (same /v1/widget payload).
* Receipt scanning flow in the app (endpoint already exists).
* IGA scraper once we know which Perth stores use igashop.

## Feature pack (barcodes, photos, plans, alerts)

### Scanning and onboarding

`GET /v1/barcode/{barcode}` resolves through a `barcodes` index rather than one
"canonical" column, because the same can arrives as EAN-13, GTIN-14 (Woolworths
prefixes a packaging `1`), UPC-A (leading zero) and, occasionally, a retailer's own
code that is not a valid GTIN at all (`11220000063348`). Every observed form maps
to a product, so any of them resolves. `checksum_ok` in the response tells the app
whether the code is a real EAN/GTIN (some Woolworths codes are not) - resolve it
anyway, but do not trust it as identity.

Unknown code -> `POST /v1/products/pending` with the name and size printed on the
can. That creates a `status='pending'` product (plus an optional price row) and
notifies nobody; an admin confirms with `/v1/admin/products/{slug}/approve`, which
can also set `caffeine_mg_per_100ml` or merge into an existing product.

Open Food Facts is only a *suggestion*: Australian energy drinks are almost absent
(7 of 8 real AU barcodes tested 404), and its entries for junk barcodes are
user-invented. Never auto-create from OFF.

### Photos

`POST /v1/photos` (multipart `file`) takes price-tag / shelf shots. Images are
re-encoded through Pillow, which strips EXIF including GPS, resized to 1600px and
stored as JPEG under `PHOTOS_DIR` as `YYYY/MM/<token>.jpg`; `GET /v1/photos/{token}`
serves them. Uploads are capped at 8 MB and de-duplicated per device by SHA-256.

**Storage location:** set `PHOTOS_HOST_DIR` in `.env` to the redmax backup disk
mount (e.g. `PHOTOS_HOST_DIR=/mnt/redmax/drinkwatch-photos`) and recreate the api
container. The container always sees it at `/srv/photos` (`PHOTOS_DIR`). Until
redmax is mounted it falls back to `./photos` on powermax.

NFC e-ink shelf labels are **not readable** by a phone: retail ESLs (Solum,
Hanshow, Pricer) use proprietary 2.4 GHz/IR/sub-GHz links and their NFC coils are
for commissioning, with vendor-encrypted payloads. Price capture is camera-based
(OCR on device) plus QR codes where a store prints them; any NDEF payload the app
does encounter should be logged as experimental data rather than trusted.

### Plans

* `POST /v1/basket/compare` - price a shopping list at each nearby store, using
  whichever pack size / multibuy is cheapest for the requested quantity (integer
  coin-change per store, so "4 singles vs a 4-pack vs 2 x 2-pack deal" is solved,
  not guessed). Also returns a multi-store plan.
* `GET /v1/value/max-caffeine?budget_cents=&mode=single_store|multi_store` - an
  exact unbounded knapsack over bundles (max mg subject to budget). Because AU
  formulated caffeinated beverages are capped at 32 mg/100 mL, this ranks by
  millilitres per dollar in practice; the API says so in `note` and flags
  `exceeds_daily_limit` against a 400 mg/day guideline.
* `GET /v1/plan/route?items=slug:qty,...` - one store per item, nearest-neighbour
  stop order, distance, and a **fuel cost estimate from the FuelWatch WA RSS feed**
  (open government data, ULP per suburb) using `VEHICLE_L_PER_100KM`. It also
  returns the cheapest single-store baseline so the app can say "not worth the
  drive".

### Alerts, notifications, caffeine

`POST /v1/alerts` stores a threshold (per product, family, chain or store).
`app/notify.py` evaluates them after every scrape and every 30 minutes, writing
`notifications` rows that the app polls with `GET /v1/notifications` (one alert
fires at most once per 24 h). Push delivery needs your own Firebase project
credentials - the payload is already shaped for an FCM data message.

`POST /v1/caffeine` + `GET /v1/caffeine/today` keep a per-device caffeine log
against the 400 mg/day guideline.

### Badges and history

`price_daily` holds one row per (day, chain, product) because `observations`
dedupes unchanged prices and would otherwise record no history. `/v1/prices`
offers carry `stats: {median_90d_cents, min_90d_cents, deal}` where `deal` is
`great|good|typical|high`. It needs a few days of rows before it says anything
useful; below three samples per chain the field is `null` on purpose.

### Home Assistant

`scripts/ha_bridge.py` publishes each tracked drink's cheapest price to MQTT with
Home Assistant discovery, so entities appear automatically and can drive
automations ("notify when Monster 4-pack < $8"). See the file docstring for env
vars (`MQTT_HOST`, `MQTT_USER`, `MQTT_PASS`, `DW_PRODUCTS`, `DW_LAT`, `DW_LON`).

### Wear OS / Android Auto

Wear OS: a Tile plus a complication, both fed by the same `/v1/widget` payload,
pushed from the phone over the Data Layer API. Platform refresh limits are
minutes, which is fine for daily prices.

Android Auto: not possible for this app category. The Car App Library accepts
navigation, POI, IOT, messaging, calling, weather, media and parked
(video/games/browsers) apps only - a price lookup fits none, so car-app review
would reject it. The useful in-car move is a "navigate to cheapest store" intent
into Google Maps.

## Design choices that would need revisiting for other product categories

Everything is modelled around canned drinks. If this expands to snacks, coffee
bags, or anything sold by weight, these are the load-bearing assumptions:

1. **`products.size_ml` and `pack_count`** are the only size model. Snacks need
   grams/pieces and a unit basis (`unit_kind` + `unit_size`), and every
   `per_litre_cents` / `caffeine_mg_for` / bundle calculation becomes
   category-specific. `caffeine_mg_per_100ml` is meaningless outside drinks;
   it should become a generic `per_100_units` nutrient block.
2. **`family_slug`** is built as `brand + flavour + size` and strips a `-xN`
   suffix. Snacks have flavours *and* formats (multipack, share pack, single
   serve) and often no size at all, so family grouping needs category rules.
3. **`scrape/*` keyword lists and the `is_energy_item` guard** are energy-drink
   specific (brand allowlists, `\benergy\b` heuristics). Each new category needs
   its own brand allowlist plus a category-scoped URL (spudshed `/category/...`,
   Woolworths/Coles search terms), and the product-matching thresholds were tuned
   on can names.
4. **Caffeine/`max-caffeine`** is an energy-drink endpoint. A generic version
   would be "max <nutrient> per dollar" with the nutrient as a parameter, and the
   FSANZ 32 mg/100 mL default becomes one of several defaults.
5. **`price_scope`** and the chain-vs-store split hold for any grocery item, but
   "is this a good deal" percentiles assume frequent repricing. Perishables and
   long-cycle goods need different windows (`price_daily` history).
6. **UI copy and defaults**: the widget's default catalogue is "8 most observed
   energy drinks, max 2 per brand". Multi-category needs per-category default
   lists and a "what do you actually buy" profile.
7. **`daily_caffeine_limit_mg`** is a health assumption for drinks only; a snack
   or iced-coffee expansion needs per-category guidance (sugar, kJ, serving size).
8. **Barcode index** stays valid, but snacks often have no GTIN (deli, produce) -
   those need a "no barcode" submission path keyed on photo + name + price/kg.
