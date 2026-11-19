"""seed.py must not undo the SpudShed retirement.

seed_spudshed.py deactivates the OSM entries the SpudShed store chooser does not
list. seed.py used to set ``is_active = True`` on every row it matched by its
``osm:`` ref, so the two documented setup steps cancelled each other: the stale
rows came back, Baldivis was scraped twice under two store ids, and ten dead
www.spudshed.com.au pages made every SpudShed run end in ``status = error``.

These tests pin insert-only activation plus the authoritative-chain skip against
an in-memory SQLite database; chains/stores use no Postgres-only types.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Chain, Store
from scripts import seed

AUTHORITATIVE_REFS = {
    "spudshed:armadale",
    "spudshed:bentleyplaza",
    "spudshed:joondalup",
    "spudshed:kalgoorlie",
    "spudshed:midland",
    "spudshed:waterford",
    "spudshed:willetton",
}

# The 16 stale rows from the audit: five published no website at all, ten
# pointed at the dead www.spudshed.com.au locator, and one was an own-subdomain
# duplicate of the live store (554).
NO_WEBSITE_IDS = (216, 336, 372, 386, 546)
DUPLICATE_SUBDOMAIN_ID = 429
DEAD_LOCATOR_IDS = (140, 160, 188, 215, 217, 218, 221, 235, 405, 450)
STALE_OSM_IDS = (*NO_WEBSITE_IDS, DUPLICATE_SUBDOMAIN_ID, *DEAD_LOCATOR_IDS)


def _stale_website(node_id: int) -> str | None:
    if node_id == DUPLICATE_SUBDOMAIN_ID:
        return "https://baldivis.spudshed.com.au"
    if node_id in DEAD_LOCATOR_IDS:
        return "https://www.spudshed.com.au"
    return None


def _stale_elements() -> list[dict]:
    """Overpass keeps returning these nodes; that is the whole problem."""
    elements: list[dict] = []
    for index, node_id in enumerate(STALE_OSM_IDS):
        tags = {
            "name": f"SpudShed Store {node_id}",
            "shop": "supermarket",
            "addr:suburb": f"Suburb {node_id}",
        }
        website = _stale_website(node_id)
        if website:
            tags["website"] = website
        elements.append(
            {
                "type": "node",
                "id": node_id,
                "lat": -32.0 - index * 0.01,
                "lon": 115.9 + index * 0.01,
                "tags": tags,
            }
        )
    return elements


def _osm_element(node_id: int, name: str, shop: str = "supermarket") -> dict:
    return {
        "type": "node",
        "id": node_id,
        "lat": -31.5 + node_id * 0.001,
        "lon": 115.8 + node_id * 0.001,
        "tags": {"name": name, "shop": shop, "addr:suburb": "Testville"},
    }


def _make_session() -> Session:
    engine = create_engine("sqlite://")
    Chain.__table__.create(engine)
    Store.__table__.create(engine)
    return Session(engine, autoflush=False, future=True)


@pytest.fixture()
def session() -> Iterator[Session]:
    """The live shape: seven authoritative rows plus the 16 retired OSM rows."""
    with _make_session() as db:
        chain = Chain(
            code="spudshed", name="SpudShed", kind="supermarket", scrape_enabled=True
        )
        db.add(chain)
        db.flush()
        db.add_all(
            [
                Store(
                    chain_id=chain.id,
                    external_ref=ref,
                    name=f"SpudShed {ref.split(':', 1)[1].title()}",
                    suburb=ref.split(":", 1)[1].title(),
                    state="WA",
                    is_active=True,
                )
                for ref in AUTHORITATIVE_REFS
            ]
        )
        db.add_all(
            [
                Store(
                    chain_id=chain.id,
                    external_ref=f"osm:node/{node_id}",
                    name=f"SpudShed Store {node_id}",
                    suburb=f"Suburb {node_id}",
                    state="WA",
                    website=_stale_website(node_id),
                    is_active=False,
                )
                for node_id in STALE_OSM_IDS
            ]
        )
        db.commit()
        yield db


@pytest.fixture()
def fresh_session() -> Iterator[Session]:
    """A database that has only seen seed.py, never seed_spudshed.py."""
    with _make_session() as db:
        db.add(
            Chain(code="spudshed", name="SpudShed", kind="supermarket", scrape_enabled=True)
        )
        db.commit()
        yield db


def _scope(session: Session):
    @contextmanager
    def _session_scope() -> Iterator[Session]:
        yield session
        session.commit()

    return _session_scope


def _run(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    elements: list[dict],
    argv: list[str],
) -> int:
    monkeypatch.setattr(seed, "init_db", lambda: None)
    monkeypatch.setattr(seed, "session_scope", _scope(session))
    monkeypatch.setattr(seed, "fetch_osm", lambda bbox: elements)
    return seed.main(argv)


def _active_refs(session: Session) -> set[str]:
    return set(session.scalars(select(Store.external_ref).where(Store.is_active.is_(True))))


def test_rerun_keeps_the_retired_osm_rows_inactive(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(session, monkeypatch, _stale_elements(), ["--no-probe"]) == 0

    assert _active_refs(session) == AUTHORITATIVE_REFS
    store_count = len(list(session.scalars(select(Store.id))))
    assert store_count == len(AUTHORITATIVE_REFS) + len(STALE_OSM_IDS)
    output = capsys.readouterr().out
    assert f"skipped {len(STALE_OSM_IDS)} OSM rows" in output


def test_rerun_does_not_touch_the_retired_rows(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A surrogate suburb that only an upsert could replace: the retired row must
    # not be refreshed from OSM at all, not merely left inactive.
    stale = session.scalar(select(Store).where(Store.external_ref == "osm:node/429"))
    assert stale is not None
    stale.suburb = "Retired Suburb"
    session.commit()

    assert _run(session, monkeypatch, _stale_elements(), ["--no-probe"]) == 0

    session.refresh(stale)
    assert stale.suburb == "Retired Suburb"
    assert stale.website == "https://baldivis.spudshed.com.au"
    assert stale.is_active is False


def test_no_subdomain_probe_runs_once_the_chain_is_authoritative(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unexpected_probe(*args: object, **kwargs: object) -> None:
        raise AssertionError("probed a chain whose store list is authoritative")

    monkeypatch.setattr(seed, "reverse_suburb", _unexpected_probe)
    monkeypatch.setattr(seed, "discover_spudshed_subdomain", _unexpected_probe)

    # No --no-probe: the skip must happen before the network work.
    assert _run(session, monkeypatch, _stale_elements(), []) == 0
    assert _active_refs(session) == AUTHORITATIVE_REFS


def test_osm_rows_are_still_seeded_before_the_authoritative_list_exists(
    fresh_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    elements = [
        _osm_element(9001, "SpudShed Baldivis"),
        _osm_element(9002, "Woolworths Baldivis"),
    ]

    assert _run(fresh_session, monkeypatch, elements, ["--no-probe"]) == 0

    assert _active_refs(fresh_session) == {"osm:node/9001", "osm:node/9002"}


def test_an_inactive_row_stays_inactive_without_an_authoritative_list(
    fresh_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = Chain(
        code="woolworths", name="Woolworths", kind="supermarket", scrape_enabled=True
    )
    fresh_session.add(chain)
    fresh_session.flush()
    fresh_session.add(
        Store(
            chain_id=chain.id,
            external_ref="osm:node/9002",
            name="Woolworths Baldivis",
            state="WA",
            is_active=False,
        )
    )
    fresh_session.commit()

    assert (
        _run(fresh_session, monkeypatch, [_osm_element(9002, "Woolworths Baldivis")], ["--no-probe"])
        == 0
    )

    store = fresh_session.scalar(select(Store).where(Store.external_ref == "osm:node/9002"))
    assert store is not None
    assert store.is_active is False


def test_authoritative_chains_ignores_a_chain_with_only_osm_rows(
    fresh_session: Session,
) -> None:
    assert seed.authoritative_chains(fresh_session) == set()


def test_authoritative_chains_detects_the_chooser_rows(session: Session) -> None:
    assert seed.authoritative_chains(session) == {"spudshed"}


def test_upsert_store_activates_only_the_row_it_creates(session: Session) -> None:
    chain = session.scalar(select(Chain).where(Chain.code == "spudshed"))
    row = {
        "external_ref": "osm:node/777",
        "name": "SpudShed New Store",
        "suburb": "Newville",
        "state": "WA",
        "postcode": "6000",
        "lat": -32.0,
        "lon": 115.9,
        "website": "https://www.spudshed.com.au",
    }

    assert seed.upsert_store(session, chain, row) is True
    session.commit()
    store = session.scalar(select(Store).where(Store.external_ref == "osm:node/777"))
    assert store is not None
    assert store.is_active is True

    store.is_active = False
    session.commit()
    assert seed.upsert_store(session, chain, row) is False
    session.commit()
    session.refresh(store)
    assert store.is_active is False
