"""Device-token auth, admin auth and in-process rate limiting."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque

from fastapi import Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Device


def new_device_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "expected 'Bearer <device-token>'")
    return parts[1].strip()


def current_device(
    authorization: str | None = Header(default=None),
    *,
    session: Session,
) -> Device:
    """Resolve the caller from a device token; touches last_seen_at."""
    token = _bearer(authorization)
    device = session.scalar(select(Device).where(Device.token_hash == hash_token(token)))
    if device is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown device token; register first")
    if device.is_banned:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "device is banned")
    device.last_seen_at = _utcnow()
    return device


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = settings.admin_token
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ADMIN_TOKEN is not configured")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad admin token")


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


class RateLimiter:
    """Sliding-window limiter. Process-local: fine for a single API container."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        self.charge(key, 1)

    def charge(self, key: str, n: int = 1) -> None:
        """Take n slots at once, or none at all.

        A batch has to be admitted as one decision. Charging it line by line
        would spend the hour on the first lines and then 429 the rest, leaving
        the client with a half-written docket it cannot resubmit; one atomic
        charge either fits the remaining window or refuses the request before
        anything is written.
        """
        if n <= 0:
            return
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) + n > self.limit:
                retry = round(self.window - (now - bucket[0]), 1) if bucket else self.window
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    f"rate limit reached; retry in {retry}s",
                    headers={"Retry-After": str(max(1, int(retry)))},
                )
            bucket.extend([now] * n)
            if len(self._hits) > 20000:  # crude memory guard
                for stale in [k for k, v in self._hits.items() if not v][:1000]:
                    self._hits.pop(stale, None)


write_limiter = RateLimiter(limit=60, window_seconds=3600)  # crowd writes per device/hour
read_limiter = RateLimiter(limit=600, window_seconds=60)  # reads per IP/minute
