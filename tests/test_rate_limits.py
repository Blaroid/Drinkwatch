"""The documented rate limits, and the buckets they are actually charged to.

API.md promised 600 reads/minute/IP and 60 writes/hour/device. Neither was
enforced: no read route touched the read limiter, so the public read API was
unmetered, and the write limiter was keyed on a per-endpoint label ("obs:17",
"vote:17", ...), so one device got six separate 60/hour quotas. The IP the
registration bucket was keyed on also came straight from X-Forwarded-For.

These tests drive the ASGI middleware directly - a scope in, a status out - so
they stay offline like the rest of the suite: no server, no database.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from app import main
from app.config import Settings
from app.security import DeviceWriteLimiter, RateLimiter, ReadRateLimitMiddleware, write_limiter

# The key shapes the write routes hand to the limiter, one per endpoint. Before
# the fix each of these was its own bucket, which is the defect: 6 x 60 writes
# per hour for a single device.
WRITE_KEYS = ("obs:7", "vote:7", "alert:7", "photo:7", "pending:7", "caffeine:7")


class EchoApp:
    """Inner ASGI app that answers 200 and keeps the scopes it was given."""

    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send) -> None:
        self.scopes.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    def forwarded_for(self, index: int) -> list[str]:
        return [
            value.decode("latin-1")
            for key, value in self.scopes[index]["headers"]
            if key.lower() == b"x-forwarded-for"
        ]


def exchange(
    middleware: ReadRateLimitMiddleware,
    *,
    path: str = "/v1/chains",
    method: str = "GET",
    client: tuple[str, int] | None = ("203.0.113.9", 51234),
    forwarded_for: str | None = None,
) -> tuple[int, list[tuple[bytes, bytes]]]:
    """One request through the middleware; returns its status and headers."""
    headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": client,
        "server": ("testserver", 80),
    }
    statuses: list[int] = []
    response_headers: list[tuple[bytes, bytes]] = []

    async def send(message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
            response_headers.extend(message["headers"])

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    asyncio.run(middleware(scope, receive, send))
    return statuses[0], response_headers


def call(middleware: ReadRateLimitMiddleware, **kwargs: Any) -> int:
    return exchange(middleware, **kwargs)[0]


def limiter(limit: int, hops: int = 0) -> tuple[ReadRateLimitMiddleware, EchoApp]:
    inner = EchoApp()
    return (
        ReadRateLimitMiddleware(
            inner, limiter=RateLimiter(limit=limit, window_seconds=60), trusted_proxy_hops=hops
        ),
        inner,
    )


def test_reads_are_charged_per_ip_and_the_next_one_is_refused() -> None:
    middleware, inner = limiter(3)

    assert [call(middleware) for _ in range(3)] == [200, 200, 200]
    assert call(middleware) == 429
    # The refusal happens before the router, so the route never ran.
    assert len(inner.scopes) == 3

    # A different caller still has its own minute.
    assert call(middleware, client=("198.51.100.4", 40000)) == 200


def test_a_refused_read_tells_the_client_when_to_retry() -> None:
    middleware, _ = limiter(3)
    for _ in range(3):
        call(middleware)

    status, headers = exchange(middleware)

    assert status == 429
    assert (b"retry-after", b"60") in headers


def test_only_v1_reads_are_charged() -> None:
    middleware, _ = limiter(3)

    for _ in range(3):
        call(middleware)
    # Writes and non-/v1 routes are metered elsewhere (or not at all); they must
    # not eat the caller's read minute.
    assert call(middleware, method="POST") == 200
    assert call(middleware, path="/healthz") == 200
    assert call(middleware, path="/app/version.json") == 200
    # /v1 itself is not a route; the documented budget is for /v1/ reads.
    assert call(middleware, path="/v1") == 200
    assert call(middleware) == 429


def test_x_forwarded_for_is_ignored_without_trusted_proxies() -> None:
    middleware, inner = limiter(3)

    # A client rotating a spoofed header must not be handed a fresh minute.
    for index in range(3):
        assert call(middleware, forwarded_for=f"10.0.0.{index}") == 200
    assert call(middleware, forwarded_for="10.0.0.99") == 429

    # Nor may it steer a downstream bucket: everything the handlers see is the
    # socket peer.
    assert inner.forwarded_for(0) == ["203.0.113.9"]
    assert inner.forwarded_for(1) == ["203.0.113.9"]


def test_forwarded_for_is_believed_up_to_the_configured_hops() -> None:
    middleware, inner = limiter(2, hops=1)

    # One proxy: the rightmost entry is the peer it saw, while the entries to
    # its left are whatever the client sent and must not mint buckets.
    call(middleware, forwarded_for="1.1.1.1, 198.51.100.7")
    call(middleware, forwarded_for="2.2.2.2, 198.51.100.7")
    assert call(middleware, forwarded_for="3.3.3.3, 198.51.100.7") == 429
    assert call(middleware, forwarded_for="3.3.3.3, 198.51.100.8") == 200

    # The handler sees only the resolved client, not the spoofable prefix.
    assert inner.forwarded_for(0) == ["198.51.100.7"]


def test_a_chain_shorter_than_the_trusted_proxies_falls_back_to_the_peer() -> None:
    middleware, inner = limiter(1, hops=2)

    assert call(middleware, forwarded_for="198.51.100.7") == 200
    assert call(middleware, forwarded_for="198.51.100.7") == 429
    assert inner.forwarded_for(0) == ["203.0.113.9"]


def test_the_app_mounts_the_read_limit_middleware() -> None:
    # The defect was a documented limit no route spent; wiring it into the app
    # is the half of the fix a unit test of the class cannot see.
    assert any(entry.cls is ReadRateLimitMiddleware for entry in main.app.user_middleware)


def test_the_default_configuration_trusts_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    assert Settings.load().trusted_proxy_hops == 0

    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    assert Settings.load().trusted_proxy_hops == 1


@pytest.fixture()
def device_hour(monkeypatch: pytest.MonkeyPatch) -> DeviceWriteLimiter:
    """The production write limiter, with its hour shrunk to the evidence case."""
    assert isinstance(write_limiter, DeviceWriteLimiter)
    monkeypatch.setattr(write_limiter, "limit", len(WRITE_KEYS))
    monkeypatch.setattr(write_limiter, "_hits", {})
    return write_limiter


def test_one_device_spends_one_hour_across_every_write_route(
    device_hour: DeviceWriteLimiter,
) -> None:
    for key in WRITE_KEYS:
        device_hour.check(key)

    # Six routes, one quota: the seventh write of the hour is refused whichever
    # endpoint it goes to.
    for key in WRITE_KEYS:
        with pytest.raises(HTTPException) as caught:
            device_hour.check(key)
        assert caught.value.status_code == 429


def test_one_devices_writes_do_not_spend_anothers(device_hour: DeviceWriteLimiter) -> None:
    for key in WRITE_KEYS:
        device_hour.check(key)

    for key in WRITE_KEYS:
        device_hour.check(key.rsplit(":", 1)[0] + ":8")


def test_a_batch_is_still_charged_once_for_its_whole_docket(
    device_hour: DeviceWriteLimiter,
) -> None:
    # The batch path charges n slots in one decision, and that decision now
    # lands in the same per-device bucket as an ordinary sighting.
    device_hour.charge("obs:7", n=len(WRITE_KEYS) - 1)
    device_hour.check("vote:7")

    with pytest.raises(HTTPException) as caught:
        device_hour.charge("obs:7", n=1)
    assert caught.value.status_code == 429
