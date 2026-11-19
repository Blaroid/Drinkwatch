# DrinkWatch API

Base URL: https://cans.hazzardous.online
Interactive docs: /docs - OpenAPI: /openapi.json

All money is integer cents. All timestamps are ISO 8601 UTC.
Read endpoints are public. Writes need a device token. Rate limits:
600 reads/minute/IP, 60 writes/hour/device.

## Widget contract (the thing the app should call)

    GET /v1/widget?lat=-31.9523&lon=115.8613&radius_km=15&products=monster-green-500ml,v-original-500ml

Optional `products` (max 8 slugs). Without it you get the 8 most widely stocked
energy drinks. Optional `max_age_hours` (default 72).

    200
    {
      "generated_at": "2026-11-18T06:07:52Z",
      "radius_km": 15.0,
      "items": [
        {
          "slug": "monster-green-500ml",
          "label": "Monster Green 500ml",
          "price_cents": 315,
          "price": "$3.15",
          "per_can_cents": null,
          "store": "SpudShed Ballajura",
          "chain": "spudshed",
          "suburb": "Ballajura",
          "distance_km": 0.0,
          "promo": "none",
          "source": "crowd",
          "price_scope": "store",
          "age_hours": 0.0
        }
      ]
    }

Payload is small on purpose (well under 2 KB for 8 items). `source` is `crowd`
or `scrape`; `price_scope` is `store` for a price seen on that shop's shelf and
`chain` when it is the chain's own online price attributed to the shop. Mark a
`chain` row (the old clients inferred it from `source`, which called every
scraped SpudShed store price "online").

## Reads

    GET /v1/meta
    GET /v1/chains
    GET /v1/stores/nearby?lat&lon&radius_km=15&chain=spudshed&limit=50
    GET /v1/products?q=monster&brand=Monster&limit=50&offset=0
    GET /v1/products/{slug}
    GET /v1/products/barcode/{barcode}
    GET /v1/prices?product={slug}&lat&lon&radius_km&max_age_hours=72&chain=
    GET /v1/compare?products=slug1,slug2&lat&lon&radius_km
    GET /v1/observations?product={slug}&store_id=&source=crowd&limit=50
    GET /v1/observations?include_flagged=true   // also needs_review rows

`/v1/products`, `/v1/catalogue`, `/v1/deals` and the widget's default and
suggestion lists only contain `verified` products, so a crowd-submitted row
waiting at `status=pending` cannot be picked before review.

/v1/prices returns the product plus every nearby store that has a fresh price,
cheapest first:

    {
      "product": {"slug": "monster-ultra-500ml", "brand": "Monster", "name": "Ultra", "size_ml": 500, "pack_count": 1, ...},
      "cheapest": { ...same shape as offers[0]... },
      "offers": [
        {
          "store_id": 15,
          "store_name": "Woolworths Perth Cultural Centre",
          "chain_code": "woolworths",
          "chain_kind": "supermarket",
          "suburb": "Perth",
          "distance_km": 0.11,
          "price_cents": 350,
          "price": "$3.50",
          "per_can_cents": null,
          "per_litre_cents": 700,
          "promo_kind": "special",
          "promo_qty": null,
          "promo_total_cents": null,
          "in_stock": true,
          "source": "scrape",
          "price_scope": "chain",
          "age_hours": 1.2,
          "observed_at": "..."
        }
      ]
    }

`promo_kind`: none | special | multibuy | clearance. For `multibuy`,
`promo_qty` and `promo_total_cents` describe the deal ("2 for $21").
`per_can_cents` is set for multipacks, `per_litre_cents` always when the size is known.

## Crowd writes

Register once per install, store the token in EncryptedSharedPreferences:

    POST /v1/devices
    {"platform": "android", "app_version": "0.1.0", "display_name": "Blaine"}
    201 {"id": 7, "token": "....", "display_name": "Blaine", "reputation": 0, "created_at": "..."}

Token is shown once. Send it as `Authorization: Bearer <token>`.

Submit a sighting:

    POST /v1/observations
    Authorization: Bearer <token>
    {
      "store_id": 556,
      "product_slug": "monster-green-500ml",   // or "barcode": "9300675012345"
      "price_cents": 315,
      "was_price_cents": null,
      "promo_kind": "none",                    // none|special|multibuy|clearance
      "promo_qty": null,
      "promo_total_cents": null,
      "in_stock": true,
      "note": "fridge by the door",
      "photo_url": null,
      "photo_token": null,                     // from POST /v1/photos (preferred)
      "observed_at": "2026-11-18T06:07:52Z"    // optional, defaults to now
    }
    201 {"id": 21448, "status": "accepted", "confirms": 0, "disputes": 0, "sightings": 1, ...}

Rules the API enforces: price 1c..$500, `observed_at` within the last 30 days and
not in the future, multibuy needs `promo_qty`. Repeat submissions of the same
price by the same device at the same store within 12h return the existing row
(201, same id) instead of duplicating. A price that is a far outlier against the
recent prices from the same chain -- widened to the same retail format
(supermarket / convenience / servo) when that chain has too little history, and
never counting the reporter's own rows -- comes back with `status:
"needs_review"` until two other users confirm it.

`sightings` is how many reports back that exact price at that store within the
hour before it. Different devices reporting the same price are the same fact, so
`GET /v1/observations` folds them into the newest report and says
`"sightings": 5` rather than showing five identical rows; the write response
carries the full tally too. Each report is still stored, so the count is the
crowd agreeing with itself rather than a repeated click by one device.

The feed only lists `status: "accepted"` rows, because a `needs_review` outlier
is not a price any other endpoint will quote. Every row carries its `status`,
and `include_flagged=true` also returns the quarantined rows so a client can
badge them instead of hiding them.

Confirm or dispute someone else's sighting:

    POST /v1/observations/{id}/vote   {"vote": 1}   // or -1
    200 {observation}

Votes apply to crowd sightings only: a scraped row is the chain's own published
price, so voting on one returns 409 -- use `/v1/reports` to say a scraped price
is wrong. The vote being cast counts toward the decision, so the second confirm
accepts a flagged row and the third dispute flags an accepted one.

Report something broken:

    POST /v1/reports  {"observation_id": 21448, "reason": "wrong_price", "note": "..."}

Your own history and profile:

    GET /v1/me
    GET /v1/me/observations?limit=50

## Admin (X-Admin-Token header)

    GET  /v1/admin/stats
    POST /v1/admin/scrape/run        {"chains": ["spudshed"]}
    GET  /v1/admin/runs
    GET  /v1/admin/listings?unmapped=1&chain=woolworths
    POST /v1/admin/listings/{id}/map {"product_slug": "..."}
    POST /v1/admin/products          {brand, name, size_ml, pack_count, barcode}
    POST /v1/admin/products/{slug}/link {"target_slug": "...", "unlink": false}
    GET  /v1/admin/products/links
    GET  /v1/admin/products/suggestions?brand=Monster&min_score=0.34
    POST /v1/admin/observations/{id}/status {"status": "accepted|needs_review|rejected"}
    GET  /v1/admin/devices

A status change is applied to the daily price ledger too, so approving a flagged
price adds it to the deal history and rejecting one takes it back out.

## Errors

    {"detail": "unknown product slug: foo"}    // 404, 422, 409, 429, 401, 403

429 responses carry `Retry-After`. 503 on /v1/admin/* means ADMIN_TOKEN is unset.


## Scanning and onboarding

    GET /v1/barcode/{barcode}
    200 {"found": true, "barcode": "19342866000496", "normalised": "9342866000496",
         "checksum_ok": true, "product": {...}}
    200 {"found": false, "barcode": "9300633099999", "normalised": "9300633099999",
         "checksum_ok": false, "suggestion": null,
         "pending_hint": "No match. Ask the user for the name and size on the can, then POST /v1/products/pending"}

Accepts EAN-13, GTIN-14 (Woolworths' 14-digit codes), UPC-A and retailer-internal
codes; all forms of the same product resolve. `checksum_ok` is advisory - some
retailer codes are not valid GTINs.

    POST /v1/products/pending            (device auth)
    {"barcode": "9300633099999", "name": "V Energy Berry 500mL", "brand": "V",
     "size_ml": 500, "pack_count": 1, "photo_token": "...", "store_id": 556,
     "price_cents": 350, "note": "front fridge"}
    201 {"product": {...}, "created": true, "observation_id": 21448}

Creates a `pending` product (name is required - that is the point) and optionally a
price sighting. If the barcode already resolves, it returns that product with
`created: false` and just attaches the price.

## Photos

    POST /v1/photos        (device auth, multipart form field "file")

    201 {"id": 3, "token": "tRnEh5kz...", "url": "https://.../v1/photos/tRnEh5kz...",
         "thumb_url": "https://.../v1/photos/tRnEh5kz.../thumb",
         "content_type": "image/jpeg", "bytes": 311, "width": 64, "height": 64,
         "created_at": "..."}

Max 8 MB, images only. EXIF (including GPS) is stripped by re-encoding; the stored
copy is capped at 1280 px and a 320 px thumbnail is written next to it. Same image
from the same device returns the existing row (unless that row was pruned).

    GET /v1/photos/{token}         -> image/jpeg (immutable cache), 404 unknown, 410 gone or pruned
    GET /v1/photos/{token}/thumb   -> image/jpeg, 320 px; backfilled on first read for old rows

A pruned row (local cache passed its 14 day retention) answers 410, never 404, so a
client can tell "gone" apart from a URL it built wrongly.

## Plans

    POST /v1/basket/compare
    {"items": [{"slug": "monster-ultra-500ml", "quantity": 4},
               {"slug": "v-blue-250ml", "quantity": 2}],
     "lat": -31.9523, "lon": 115.8613, "radius_km": 15}

`slug` may be a product slug or a family slug (pack size folded away). Each store
gets the cheapest legal combination of singles, packs and multibuys:

    200 {"stores": [{"store_name": "Woolworths Perth Cultural Centre",
                     "total_cents": 1910, "total": "$19.10", "complete": true,
                     "distance_km": 0.11,
                     "items": [{"requested": "monster-ultra-500ml", "units": 4,
                                "product_slug": "monster-ultra-500ml",
                                "label": "Monster Ultra 500ml",
                                "cost_cents": 1380, "price": "$13.80",
                                "breakdown": ["4 x single @ $3.45"]}]}],
         "best_single_store": {...}, "multi_store_total_cents": 1910,
         "multi_store_plan": [{"requested": "...", "store_name": "...", "cost_cents": 875}],
         "savings_vs_worst_cents": 3988}

Stores that cannot cover every item come back with `complete: false` and sort last.

`product_slug` and `label` name the pack the optimiser actually bought, so they
always agree with `cost_cents` and `breakdown`: a plan of two singles is labelled
as the single, never as the x4 pack. `/v1/plan/route` items carry the same
`BasketLineOut` fields (`units`, `price`, ...) and keep `cost` as a deprecated
alias of `price` for builds that still read it.

    GET /v1/value/max-caffeine?budget_cents=2000&lat=-31.9523&lon=115.8613
        &radius_km=25&mode=single_store|multi_store&include_specials=true

    200 {"budget_cents": 2000, "mode": "single_store", "spent_cents": 1740,
         "leftover_cents": 260, "total_caffeine_mg": 1280, "total_ml": 4000,
         "items": [{"slug": "mother-original-500ml-x4", "units": 8,
                    "cost": "$17.40", "caffeine_mg": 1280,
                    "store_name": "Woolworths Perth Cultural Centre", "chain_code": "woolworths"}],
         "note": "Caffeine assumes 32mg/100mL unless a product overrides it ...",
         "daily_limit_mg": 400, "exceeds_daily_limit": true, "stores_used": 1}

Exact integer knapsack (bundles are indivisible). Budget above `MAX_BUDGET_CENTS`
returns 422. `mode=single_store` prices every shop on its own and returns the best
one-shop plan (the nearest shop wins a tie), so every item is buyable in one trip;
`mode=multi_store` may split the budget across shops.

    GET /v1/plan/route?items=monster-ultra-500ml:4,v-blue-250ml:2&lat=&lon=&radius_km=

    {"stops": [{"store_name": "...", "subtotal_cents": 1910, "items": [...],
                "distance_from_origin_km": 0.11}],
     "items_total_cents": 1910, "travel_km": 0.2, "fuel_price_cents_per_l": 185,
     "fuel_cost_cents": 3, "total_cost_cents": 1913,
     "single_store_baseline_cents": 1910, "net_saving_cents": -3,
     "fuel_source": "fuelwatch"}

## Alerts, notifications, caffeine

    POST   /v1/alerts      {"product_slug": "monster-ultra-500ml", "max_price_cents": 800,
                            "chain_code": null, "store_id": null}      (device auth)
           optional triggers and filters: {"min_drop_percent": 15,
                            "lat": -31.9523, "lon": 115.8613, "radius_km": 10}
    GET    /v1/alerts                                                    (device auth)
    DELETE /v1/alerts/{id}                                               (device auth, deactivates)
    GET    /v1/notifications?unread_only=true&limit=50                   (device auth)
    POST   /v1/notifications/{id}/read                                   (device auth)

    POST /v1/caffeine      {"product_slug": "monster-ultra-500ml", "qty": 1}
                            or {"label": "barista flat white", "mg_caffeine": 120, "qty": 1}
    GET  /v1/caffeine/today
    200 {"date": "2026-11-18", "total_mg": 160, "limit_mg": 400,
         "remaining_mg": 240, "entries": [...]}

Alerts fire at most once per 24 h per alert. Delivery is pull-based until you add
FCM credentials; the notification `payload` is already FCM-shaped.

There are two triggers and either is enough: `max_price_cents` (an absolute
ceiling) and `min_drop_percent` (a fall of at least that many percent below the
product's 30-day median in that chain). `lat`/`lon`/`radius_km` — all three or
none — limit evaluation to stores within that radius; without them an alert
scans every active store, as it always did. `chain_code` and `store_id` narrow
the scan further and still work alongside the radius.

## Batch and receipts

    POST /v1/observations/batch       (device auth)
    {"items": [ {<ObservationIn>}, {<ObservationIn>} ]}
    200 [{"index": 0, "status": "accepted", "observation_id": 21448},
         {"index": 1, "status": "error", "error": "unknown store"}]

    POST /v1/receipts/parse           (device auth)
    {"text": "MONSTER ULTRA 4PK 17.50\nV ENERGY BLUE 250ML 3.29\nBANANAS 4.50"}
    200 [{"line": "MONSTER ULTRA 4PK 17.50", "price_cents": 1750,
          "product_slug": "monster-ultra-500ml-x4", "label": "Monster Ultra", "score": 0.67}]

A batch is charged against the hourly crowd write quota once, for its whole
line count, rather than once per line: a receipt is one act of reporting. If
the docket does not fit the quota left in the hour the request is refused with
`429` before any row is written; a line that fails validation is still reported
per item as above and does not fail the docket.

OCR happens on the device (ML Kit); the parse endpoint only matches the text to
the catalogue so the user can confirm a whole docket at once.

## Discovery

    GET /v1/stores/search?q=spud&lat=-31.9523&lon=115.8613&limit=20

Matches store name, suburb, postcode and chain name; prefix matches rank above
substring matches, and distance annotates (and sorts) when lat/lon are given.

    GET /v1/leaderboard?period=week|month|all
    200 [{"rank": 1, "device": "Blaine", "reputation": 12, "accepted": 9}]

## Admin additions

    GET  /v1/admin/products/pending
    POST /v1/admin/products/{slug}/approve   {"size_ml": 500, "caffeine_mg_per_100ml": 32,
                                              "merge_into_slug": null}
    POST /v1/admin/products/{slug}/reject
    GET  /v1/admin/photos?limit=50
    POST /v1/admin/alerts/evaluate           -> {"notifications_created": 1}
