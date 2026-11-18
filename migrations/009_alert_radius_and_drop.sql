-- Alerts get a location filter and a relative trigger.
--
-- Two things were wrong with a fixed dollar threshold over every active store:
-- "tell me when V drops under $2.50" pinged about a servo 60 km away, and the
-- number itself goes stale as the "normal" price moves. radius_km/lat/lon limit
-- the first; min_drop_percent answers the second by alerting on any fall of that
-- many percent below the product's 30-day median.
--
-- All four columns are nullable and the absolute threshold becomes optional, so
-- existing alerts keep their exact meaning: no location, no relative trigger,
-- threshold as before (no backfill needed).

alter table price_alerts
    add column if not exists lat double precision,
    add column if not exists lon double precision,
    add column if not exists radius_km double precision,
    add column if not exists min_drop_percent smallint;

alter table price_alerts alter column max_price_cents drop not null;

-- An alert with neither trigger could never fire; one with either is fine.
alter table price_alerts
    add constraint ck_alert_trigger
    check (max_price_cents is not null or min_drop_percent is not null);

alter table price_alerts
    add constraint ck_alert_drop_percent
    check (min_drop_percent is null or min_drop_percent between 1 and 90);

-- A radius we cannot evaluate (no centre) or half a location pair is a bug, not
-- a preference: reject it at write time rather than silently scanning everything.
alter table price_alerts
    add constraint ck_alert_location
    check (
        (lat is null and lon is null and radius_km is null)
        or (
            lat is not null
            and lon is not null
            and radius_km is not null
            and lat between -90 and 90
            and lon between -180 and 180
            and radius_km > 0
        )
    );
