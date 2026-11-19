"""Distance filtering must happen before result limits are applied.

Both store lists are ranked by haversine in Python, so a SQL LIMIT placed before
that ranking cuts rows by heap order and can return a store 6 km away instead of
the one 400 m away. SQLite stands in for Postgres here: chains and stores use no
Postgres-only types, and the pre-fix bugs reproduce as rowid-order cuts.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.deps import haversine_km, nearby_stores
from app.api.public import stores_search
from app.models import Chain, Store

PERTH = (-31.9523, 115.8613)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite://")
    Chain.__table__.create(engine)
    Store.__table__.create(engine)
    with Session(engine, autoflush=False, future=True) as db:
        db.add(Chain(id=1, code="coles", name="Coles", kind="supermarket"))
        db.commit()
        yield db


def _store(db: Session, name: str, lat_offset_km: float) -> Store:
    lat, lon = PERTH
    store = Store(
        chain_id=1,
        name=name,
        lat=lat + lat_offset_km / 111.0,
        lon=lon,
        is_active=True,
        external_ref=name,
    )
    db.add(store)
    db.flush()
    return store


def test_nearby_keeps_the_closest_stores_when_the_box_holds_more_than_the_limit(
    session: Session,
) -> None:
    # Evidence case from the audit: 15 km around Perth CBD holds 277 active
    # stores, the default limit asks for 50, and Coles Northbridge (0.41 km)
    # and Foodary Perth Cultural Centre (0.87 km) were missing. Insert furthest
    # first and south of the CBD, with the closest stores north and last, so
    # both rowid order and the lat/lon index return the wrong end first.
    lat, lon = PERTH
    others = [1.0 + index * (13.5 / 274) for index in range(275)]
    distances = sorted([0.41, 0.87] + others, reverse=True)
    names = {
        0.41: "Coles Northbridge",
        0.87: "Foodary Perth Cultural Centre",
    }
    inserted = {}
    for index, distance in enumerate(distances):
        name = names.get(distance, f"Store {index}")
        lat_offset = distance if name in names else -distance
        inserted[name] = _store(session, name, lat_offset)
    session.commit()

    found = nearby_stores(session, lat, lon, 15.0, limit=50)

    expected = sorted(
        (round(haversine_km(lat, lon, store.lat, store.lon), 2), store.name)
        for store in inserted.values()
    )
    assert [name for _, name in expected[:50]] == [store.name for store, _ in found]
    assert found[0][0].name == "Coles Northbridge"
    assert found[0][1] == pytest.approx(0.41, abs=0.01)
    assert found[1][0].name == "Foodary Perth Cultural Centre"
    assert found[-1][1] <= round(expected[49][0], 2)


def test_stores_search_radius_keeps_a_match_that_a_row_limit_would_skip(
    session: Session,
) -> None:
    # q=a&radius_km=2 has to find Foodary at 0.87 km even when hundreds of
    # other matching stores (all outside the radius) sort ahead of it.
    lat, lon = PERTH
    for index in range(350):
        _store(session, f"Distant Store {index}", -11.0)
    _store(session, "Foodary Perth Cultural Centre", 0.87)
    session.commit()

    found = stores_search(q="a", lat=lat, lon=lon, radius_km=2.0, limit=20, session=session)

    assert [store.name for store in found] == ["Foodary Perth Cultural Centre"]
    assert found[0].distance_km == pytest.approx(0.87, abs=0.01)
