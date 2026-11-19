"""FuelWatch ULP parsing, and the scope of the feed request.

The regression pinned here: ulp_prices() asked the upstream for one locality
(Suburb=Perth), so the returned map only held six CBD-adjacent suburbs and every
plan outside them silently charged the fallback price. The feed is now read
unfiltered, which is why the recorded excerpt can answer for KARAWARA. The
parse also reads <location>, because the description regex needed a phone
number and phone-less sites left their whole suburb unquoted.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.scrape import fuelwatch
from app.scrape.fuelwatch import lookup_suburb, state_median_cents

FIXTURE = Path(__file__).parent / "fixtures" / "fuelwatch_ulp.xml"


def _item(suburb: str, price: str) -> str:
    return (
        "<item>"
        f"<title>{price}: Test {suburb.title()}</title>"
        f"<description>Address: 1 Test Rd, {suburb}, Phone: (08) 0000 0000</description>"
        f"<price>{price}</price>"
        f"<location>{suburb}</location>"
        "</item>"
    )


def _feed(*items: str) -> str:
    return '<rss version="2.0"><channel>' + "".join(items) + "</channel></rss>"


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _stub_feed(monkeypatch, text: str | None = None) -> list[dict]:
    """Wire ulp_prices() to the recorded feed; returns the recorded requests."""
    body = FIXTURE.read_text(encoding="utf-8") if text is None else text
    calls: list[dict] = []

    class _Client:
        """Records the feed request and replays a saved body."""

        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

        def get(self, url: str, params: dict | None = None) -> _Response:
            calls.append({"url": url, "params": dict(params or {})})
            return _Response(body)

    monkeypatch.setattr(fuelwatch, "httpx", SimpleNamespace(Client=_Client))
    monkeypatch.setattr(
        fuelwatch,
        "settings",
        SimpleNamespace(fuelwatch_enabled=True, scrape_user_agent="test"),
    )
    # The cache is process-wide; a stale map from another test would hide the fetch.
    monkeypatch.setattr(fuelwatch, "_cache", {})
    return calls


def _stub_failure(monkeypatch) -> list[dict]:
    """Wire ulp_prices() to a feed that times out; returns the attempted reads."""
    calls: list[dict] = []

    class _Client:
        """Every read fails the way the real feed does while it is down."""

        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

        def get(self, url: str, params: dict | None = None) -> _Response:
            calls.append({"url": url, "params": dict(params or {})})
            raise httpx.ConnectTimeout("feed down")

    monkeypatch.setattr(fuelwatch, "httpx", SimpleNamespace(Client=_Client))
    monkeypatch.setattr(
        fuelwatch,
        "settings",
        SimpleNamespace(fuelwatch_enabled=True, scrape_user_agent="test"),
    )
    monkeypatch.setattr(fuelwatch, "_cache", {})
    return calls


def test_parse_keeps_the_cheapest_site_per_suburb() -> None:
    prices = fuelwatch._parse(_feed(_item("KARAWARA", "203.9"), _item("KARAWARA", "199.9")))

    assert prices == {"KARAWARA": 200}


def test_parse_accepts_a_byte_order_mark() -> None:
    prices = fuelwatch._parse("\ufeff" + _feed(_item("BALDIVIS", "179.9")))

    assert prices == {"BALDIVIS": 180}


def test_parse_reads_the_location_element_when_the_phone_is_missing() -> None:
    # The address regex needs "Phone" to end the suburb; Dianella's sites have
    # no phone, and dropping them left the whole suburb on the fallback price.
    item = (
        "<item><title>183.9: Shell Express Dianella</title>"
        "<description>Address: 67 Walter Rd West, DIANELLA, Open 24 hours</description>"
        "<location>DIANELLA</location></item>"
    )

    assert fuelwatch._parse(_feed(item)) == {"DIANELLA": 184}


def test_parse_returns_nothing_for_an_unparsable_body() -> None:
    assert fuelwatch._parse("<html>not a feed") == {}


def test_ulp_prices_reads_the_feed_without_a_suburb_filter(monkeypatch) -> None:
    calls = _stub_feed(monkeypatch)

    fuelwatch.ulp_prices()

    assert len(calls) == 1
    assert calls[0]["url"] == fuelwatch.FEED_URL
    # Suburb is a locality filter upstream: sending one would cap the map to a
    # handful of suburbs instead of the whole metro.
    assert calls[0]["params"] == {"Product": 1}


def test_ulp_prices_covers_the_whole_feed_not_one_locality(monkeypatch) -> None:
    _stub_feed(monkeypatch)

    prices = fuelwatch.ulp_prices()

    assert prices == {"KARAWARA": 204, "MADDINGTON": 190, "PERTH": 204, "DIANELLA": 184}
    assert fuelwatch.price_for_suburb("karawara") == 204


def test_ulp_prices_is_cached_between_plan_requests(monkeypatch) -> None:
    calls = _stub_feed(monkeypatch)

    first = fuelwatch.ulp_prices()
    second = fuelwatch.ulp_prices()

    assert len(calls) == 1
    assert second is first


def test_a_failed_fetch_is_not_re_downloaded_within_the_backoff(monkeypatch) -> None:
    calls = _stub_failure(monkeypatch)

    assert fuelwatch.ulp_prices() == {}
    assert fuelwatch.ulp_prices() == {}
    assert fuelwatch.ulp_prices() == {}

    # One attempt, not one 20 s timeout per basket/route request.
    assert len(calls) == 1


def test_a_failed_fetch_serves_the_last_good_map(monkeypatch) -> None:
    calls = _stub_failure(monkeypatch)
    # A good map older than the normal TTL: still better than pricing on the
    # hard-coded fallback while the feed is down.
    fuelwatch._cache["product:1"] = (
        time.monotonic() - fuelwatch.CACHE_TTL_SECONDS - 1,
        {"PERTH": 204},
    )

    assert fuelwatch.ulp_prices() == {"PERTH": 204}
    assert fuelwatch.ulp_prices() == {"PERTH": 204}
    assert len(calls) == 1


def test_the_feed_is_retried_once_the_backoff_expires(monkeypatch) -> None:
    calls = _stub_feed(monkeypatch)
    fuelwatch._cache["failed:1"] = (
        time.monotonic() - fuelwatch.FAILURE_TTL_SECONDS - 1,
        {},
    )

    assert fuelwatch.ulp_prices() == {
        "KARAWARA": 204,
        "MADDINGTON": 190,
        "PERTH": 204,
        "DIANELLA": 184,
    }
    assert len(calls) == 1
    # A recovery clears the marker so the next failure is timed from then on.
    assert "failed:1" not in fuelwatch._cache


def test_store_suburb_matches_a_more_general_station_locality() -> None:
    prices = {"PERTH": 204, "SUBIACO": 190}
    # OSM place nodes are finer-grained than station localities.
    assert lookup_suburb(prices, "Perth Cultural Centre") == 204


def test_store_suburb_matches_a_more_specific_station_locality() -> None:
    prices = {"PERTH AIRPORT": 209}
    assert lookup_suburb(prices, "Perth") == 209


def test_unknown_suburb_returns_none() -> None:
    assert lookup_suburb({"PERTH": 204}, "Waterford") is None


def test_ambiguous_containment_takes_the_median_not_the_cheapest() -> None:
    # "PERTH AIRPORT WEST" contains both localities; the cheap one must not set
    # the price for the whole area.
    prices = {"PERTH": 210, "PERTH AIRPORT": 190}
    assert lookup_suburb(prices, "Perth Airport West") == 210


def test_state_median_is_the_middle_of_the_feed() -> None:
    assert state_median_cents({"A": 180, "B": 204, "C": 260}) == 204
    assert state_median_cents({}) is None
