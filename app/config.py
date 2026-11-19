"""Runtime configuration, loaded once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_token: str
    public_base_url: str
    scrape_enabled: bool
    scrape_user_agent: str
    scrape_browser_ua: str
    # Woolworths only prices per state, and its search API answers from whatever
    # store context it infers unless we name one. Set these to a WA store.
    woolworths_store_id: str
    woolworths_settlement: str
    woolworths_state: str
    scheduler_enabled: bool
    cors_origins: tuple[str, ...]
    # How many reverse proxies terminate TLS in front of the API. The read quota
    # and device registration key their buckets on the client IP, and
    # X-Forwarded-For is client-writable, so its rightmost entries are believed
    # only up to the number of proxies we assume appended them. 0 means the API
    # is exposed directly and the header is ignored.
    trusted_proxy_hops: int
    default_radius_km: float
    max_radius_km: float
    default_max_age_hours: int
    max_price_cents: int
    photos_dir: str
    apk_dir: str
    photos_max_bytes: int
    off_lookup_enabled: bool
    fuelwatch_enabled: bool
    vehicle_l_per_100km: float
    fuel_price_cents_per_l: int
    drive_factor: float
    max_budget_cents: int
    daily_caffeine_limit_mg: int

    @classmethod
    def load(cls) -> "Settings":
        origins = tuple(
            o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
        )
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL",
                "postgresql+psycopg://drinkwatch:drinkwatch@db:5432/drinkwatch",
            ),
            admin_token=os.environ.get("ADMIN_TOKEN", ""),
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8788").rstrip("/"),
            scrape_enabled=_flag("SCRAPE_ENABLED", True),
            scrape_user_agent=os.environ.get(
                "SCRAPE_USER_AGENT",
                "DrinkWatch/0.1 (personal price tracker; contact: unset)",
            ),
            scrape_browser_ua=os.environ.get(
                "SCRAPE_BROWSER_UA",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36",
            ),
            woolworths_store_id=os.environ.get("WOOLWORTHS_STORE_ID", "").strip(),
            woolworths_settlement=os.environ.get("WOOLWORTHS_SETTLEMENT", "").strip(),
            woolworths_state=os.environ.get("WOOLWORTHS_STATE", "WA").strip() or "WA",
            scheduler_enabled=_flag("SCHEDULER_ENABLED", True),
            cors_origins=origins,
            trusted_proxy_hops=max(0, int(os.environ.get("TRUSTED_PROXY_HOPS") or 0)),
            default_radius_km=float(os.environ.get("DEFAULT_RADIUS_KM", "15")),
            max_radius_km=float(os.environ.get("MAX_RADIUS_KM", "100")),
            default_max_age_hours=int(os.environ.get("DEFAULT_MAX_AGE_HOURS", "72")),
            max_price_cents=int(os.environ.get("MAX_PRICE_CENTS", "50000")),
            photos_dir=os.environ.get("PHOTOS_DIR", "/srv/photos"),
            apk_dir=os.environ.get("APK_DIR", "/srv/apk"),
            photos_max_bytes=int(os.environ.get("PHOTOS_MAX_BYTES", str(8 * 1024 * 1024))),
            off_lookup_enabled=_flag("OFF_LOOKUP_ENABLED", True),
            fuelwatch_enabled=_flag("FUELWATCH_ENABLED", True),
            vehicle_l_per_100km=float(os.environ.get("VEHICLE_L_PER_100KM", "8.5")),
            fuel_price_cents_per_l=int(os.environ.get("FUEL_PRICE_CENTS_PER_L", "180")),
            # Streets, river crossings and one-ways make a real drive ~30% longer
            # than the crow flight haversine measures. Below 1.0 would claim a car
            # can beat the straight line, so clamp it.
            drive_factor=max(1.0, float(os.environ.get("DRIVE_FACTOR", "1.3"))),
            max_budget_cents=int(os.environ.get("MAX_BUDGET_CENTS", "20000")),
            daily_caffeine_limit_mg=int(os.environ.get("DAILY_CAFFEINE_LIMIT_MG", "400")),
        )


settings = Settings.load()
