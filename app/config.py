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
    scheduler_enabled: bool
    cors_origins: tuple[str, ...]
    default_radius_km: float
    max_radius_km: float
    default_max_age_hours: int
    max_price_cents: int

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
            scheduler_enabled=_flag("SCHEDULER_ENABLED", True),
            cors_origins=origins,
            default_radius_km=float(os.environ.get("DEFAULT_RADIUS_KM", "15")),
            max_radius_km=float(os.environ.get("MAX_RADIUS_KM", "100")),
            default_max_age_hours=int(os.environ.get("DEFAULT_MAX_AGE_HOURS", "72")),
            max_price_cents=int(os.environ.get("MAX_PRICE_CENTS", "50000")),
        )


settings = Settings.load()
