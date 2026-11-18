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
          "age_hours": 0.0
        }
      ]
    }

Payload is small on purpose (well under 2 KB for 8 items). `source` is `crowd`
or `scrape`; show a "~" or "online" marker when the underlying price came from
`/v1/prices` with `price_scope=chain`.

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
      "observed_at": "2026-11-18T06:07:52Z"    // optional, defaults to now
    }
    201 {"id": 21448, "status": "accepted", "confirms": 0, "disputes": 0, ...}

Rules the API enforces: price 1c..$500, `observed_at` within the last 30 days and
not in the future, multibuy needs `promo_qty`. Repeat submissions of the same
price by the same device at the same store within 12h return the existing row
(201, same id) instead of duplicating. A price more than 2.5x (or under 0.4x) the
recent median comes back with `status: "needs_review"` until two other users
confirm it.

Confirm or dispute someone else's sighting:

    POST /v1/observations/{id}/vote   {"vote": 1}   // or -1
    200 {observation}

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

## Errors

    {"detail": "unknown product slug: foo"}    // 404, 422, 409, 429, 401, 403

429 responses carry `Retry-After`. 503 on /v1/admin/* means ADMIN_TOKEN is unset.
