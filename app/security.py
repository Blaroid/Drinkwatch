"""Device-token auth, admin auth and in-process rate limiting."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque
from typing import Any, Mapping

from fastapi import Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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


class DeviceWriteLimiter(RateLimiter):
    """One hourly bucket per device, whatever route the write came from.

    Call sites pass a per-endpoint label along with the device id ("obs:17",
    "vote:17", ...). Keying the whole string gave a device one 60/hour quota
    per endpoint, so a single client could write 6 x 60 times an hour by
    alternating routes. The label is dropped before the bucket is touched, so
    every write route spends the same hour; a bare device id already has that
    shape.
    """

    def charge(self, key: str, n: int = 1) -> None:
        super().charge(f"write:{key.rsplit(':', 1)[-1]}", n=n)


write_limiter = DeviceWriteLimiter(limit=60, window_seconds=3600)  # writes per device/hour
read_limiter = RateLimiter(limit=600, window_seconds=60)  # reads per IP/minute


READ_METHODS = frozenset({"GET", "HEAD"})
READ_LIMITED_PREFIX = "/v1/"


def _socket_peer(scope: Mapping[str, Any]) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


def _clean_ip(raw: str) -> str:
    """Drop the port and brackets an X-Forwarded-For hop may carry."""
    value = raw.strip().strip('"')
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.strip()


def _header(scope: Mapping[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _with_client_ip(
    headers: list[tuple[bytes, bytes]], client_ip: str
) -> list[tuple[bytes, bytes]]:
    """Replace the request's X-Forwarded-For with the resolved client only."""
    kept = [(key, value) for key, value in headers if key.lower() != b"x-forwarded-for"]
    kept.append((b"x-forwarded-for", client_ip.encode("latin-1")))
    return kept


class ReadRateLimitMiddleware:
    """Charge the read quota per client IP for every /v1 read.

    It sits in the ASGI stack rather than in a dependency so every /v1 route is
    covered, including the ones added later: a dependency has to be opted into
    per router, and the routers that never opted in are how the documented
    600 reads/minute went unenforced.

    The trust decision for X-Forwarded-For lives here too. Every request leaves
    this layer carrying a single resolved value, so handlers that key a bucket
    on the header (device registration) cannot be steered to an arbitrary
    bucket by a spoofed chain.
    """

    def __init__(self, app: ASGIApp, *, limiter: RateLimiter, trusted_proxy_hops: int = 0) -> None:
        self.app = app
        self.limiter = limiter
        self.trusted_proxy_hops = max(0, trusted_proxy_hops)

    def client_ip(self, scope: Mapping[str, Any]) -> str:
        """The address to bill, believing X-Forwarded-For only as far as configured."""
        peer = _socket_peer(scope)
        hops = self.trusted_proxy_hops
        if hops <= 0:
            return peer
        chain = [_clean_ip(part) for part in _header(scope, b"x-forwarded-for").split(",")]
        chain = [part for part in chain if part]
        if len(chain) < hops:
            # Shorter than the proxies we trust, so it did not come through
            # them and the header is whatever the client chose to send.
            return peer
        return chain[-hops]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Work on a copy: the server hands the same scope to the lifespan and
        # to error handling, and the sanitised headers are for this request.
        scope = dict(scope)
        client_ip = self.client_ip(scope)
        scope["headers"] = _with_client_ip(scope.get("headers") or [], client_ip)
        if scope["method"] in READ_METHODS and scope["path"].startswith(READ_LIMITED_PREFIX):
            try:
                self.limiter.check("ip:" + client_ip)
            except HTTPException as exc:
                # A 429 raised before the router exists, so answer it here
                # rather than let Starlette turn it into a 500.
                await JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)
