"""Offline tests for the alert trigger and radius rules.

evaluate_alerts() needs Postgres, but every decision it makes about *whether* a
price should ping is a pure function, and the store query can be compiled without
a connection. Those are what these tests pin, so the radius and the percent-drop
trigger can change without a database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.models import PriceAlert, Store
from app.notify import STORE_SCAN_LIMIT, _drop_percent, _in_radius, _matches, _median_window, _alert_stores


def _alert(**overrides: object) -> PriceAlert:
    defaults: dict[str, object] = {
        "device_id": 1,
        "max_price_cents": None,
        "min_drop_percent": None,
    }
    defaults.update(overrides)
    return PriceAlert(**defaults)


def _store(lat: float | None = None, lon: float | None = None) -> Store:
    return Store(chain_id=1, name="test servo", lat=lat, lon=lon)


class _RecordingSession:
    """Stand-in for Session that keeps the statement instead of executing it."""

    def __init__(self, stores: list[Store] | None = None) -> None:
        self.statement = None
        self._stores = stores or []

    def scalars(self, statement):  # noqa: ANN001 - mirrors Session.scalars
        self.statement = statement
        return iter(self._stores)


def _sql(session: _RecordingSession) -> str:
    assert session.statement is not None
    return str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# ---------- percent drop ----------


def test_drop_percent_is_measured_against_the_median() -> None:
    assert _drop_percent(250, 300) == pytest.approx(16.666, abs=0.01)


def test_no_drop_when_price_is_at_or_above_the_median() -> None:
    assert _drop_percent(300, 300) is None
    assert _drop_percent(350, 300) is None


def test_no_drop_without_history() -> None:
    # A product with no rows in price_daily has no baseline; that must not read
    # as a 0% drop and must not fire a relative alert.
    assert _drop_percent(250, None) is None
    assert _matches(_alert(min_drop_percent=10), 250, None) == (False, None)


# ---------- triggers ----------


def test_absolute_threshold_still_matches_on_its_own() -> None:
    alert = _alert(max_price_cents=250)
    assert _matches(alert, 250, None) == (True, None)
    assert _matches(alert, 251, None) == (False, None)


def test_relative_trigger_fires_on_a_real_drop() -> None:
    alert = _alert(min_drop_percent=15)
    matched, drop = _matches(alert, 250, 300)
    assert matched
    assert drop == pytest.approx(16.666, abs=0.01)


def test_relative_trigger_ignores_a_drop_that_is_too_small() -> None:
    alert = _alert(min_drop_percent=15)
    assert _matches(alert, 290, 300) == (False, None)


def test_either_trigger_is_enough_when_both_are_set() -> None:
    alert = _alert(max_price_cents=200, min_drop_percent=10)
    # Above the ceiling, but well below the median: the relative trigger carries it.
    assert _matches(alert, 250, 300)[0] is True
    # Below the ceiling, but barely below the median: the threshold carries it.
    assert _matches(alert, 195, 198)[0] is True


def test_threshold_match_still_reports_a_measured_drop() -> None:
    alert = _alert(max_price_cents=250, min_drop_percent=15)
    matched, drop = _matches(alert, 240, 300)
    assert matched
    assert drop == pytest.approx(20.0)


# ---------- radius ----------


def test_no_location_means_every_store_is_considered() -> None:
    # The pre-radius behaviour, including stores that have no coordinates at all.
    alert = _alert(max_price_cents=250)
    assert _in_radius(alert, _store(-31.95, 115.86)) is True
    assert _in_radius(alert, _store()) is True


def test_radius_includes_stores_inside_and_excludes_stores_outside() -> None:
    alert = _alert(lat=-31.9523, lon=115.8613, radius_km=5.0)
    assert _in_radius(alert, _store(-31.9480, 115.8260)) is True  # ~3.4 km
    assert _in_radius(alert, _store(-31.5000, 115.8613)) is False  # ~50 km


def test_radius_excludes_a_store_without_coordinates() -> None:
    alert = _alert(lat=-31.9523, lon=115.8613, radius_km=50.0)
    assert _in_radius(alert, _store()) is False


# ---------- store query ----------


def test_store_query_caps_the_scan_when_there_is_no_radius() -> None:
    session = _RecordingSession([_store(-31.95, 115.86)])
    _alert_stores(session, _alert(max_price_cents=250))
    sql = _sql(session).upper()
    assert "LIMIT" in sql
    assert str(STORE_SCAN_LIMIT) in sql
    # No radius: no coordinate predicates, so the scan is the old one, capped.
    assert "BETWEEN" not in sql


def test_store_query_applies_both_radius_and_chain_filter() -> None:
    session = _RecordingSession()
    _alert_stores(session, _alert(chain_code="coles", lat=-31.9523, lon=115.8613, radius_km=5.0))
    sql = _sql(session)
    assert "JOIN chains" in sql
    assert "chains.code = " in sql
    assert "stores.lat BETWEEN" in sql
    assert "stores.lon BETWEEN" in sql


def test_store_query_still_pins_an_explicit_store() -> None:
    session = _RecordingSession()
    _alert_stores(session, _alert(store_id=42, max_price_cents=250))
    assert "stores.id = " in _sql(session)


# ---------- median window ----------


def test_median_window_is_thirty_local_days_excluding_today() -> None:
    # 17:30Z is already the next day in Perth (UTC+8), so the window has to
    # follow the local date the price_daily rows were written under.
    now = datetime(2026, 1, 1, 17, 30, tzinfo=timezone.utc)
    assert _median_window(now) == (date(2025, 12, 3), date(2026, 1, 2))


def test_ceiling_trigger_still_works() -> None:
    matched, _ = _matches(_alert(max_price_cents=400), 350, None)
    assert matched is True


def test_special_trigger_fires_on_a_promotion_only() -> None:
    assert _matches(_alert(on_special=True), 500, None, promo_kind="special")[0] is True
    assert _matches(_alert(on_special=True), 500, None, promo_kind="multibuy")[0] is True
    assert _matches(_alert(on_special=True), 500, None, promo_kind="none")[0] is False


def test_drop_trigger_measures_against_the_median() -> None:
    alert = _alert(min_drop_percent=20)
    assert _matches(alert, 350, 500.0)[0] is True
    assert _matches(alert, 420, 500.0)[0] is False
    assert _drop_percent(350, 500.0) == 30.0


def test_triggers_compose() -> None:
    alert = _alert(max_price_cents=300, min_drop_percent=10, on_special=True)
    assert _matches(alert, 295, None)[0] is True  # ceiling
    assert _matches(alert, 450, 500.0, promo_kind="special")[0] is True  # special
    # 450 against a 400 median is no drop at all, so only the special trigger could fire.
    assert _matches(alert, 450, 400.0, promo_kind="none")[0] is False
