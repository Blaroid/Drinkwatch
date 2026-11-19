"""The SpudShed seeder must never retire stores on a failed read.

The chooser page is the only source of the live store list, so the final loop
used to mark every SpudShed row inactive when the parse came back empty; a
failed subdomain probe did the same to that one store. These tests pin the
abort and the retire guard against a real (in-memory) database, the same way
test_caffeine_seed does: the chains/stores tables use no Postgres-only types.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Chain, Store
from scripts import seed_spudshed

ACTIVE_REFS = {
    "spudshed:armadale",
    "spudshed:bentleyplaza",
    "spudshed:joondalup",
    "spudshed:kalgoorlie",
    "spudshed:midland",
    "spudshed:waterford",
    "spudshed:willetton",
}

ONE_CARD = """<div class="StoreCard">
  <span class="StoreCard__Name">Bentley Plaza</span>
  <span class="StoreCard__Details">1 Albany Hwy<br>Bentley WA 6102</span>
</div></div></div>"""


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _Client:
    """Minimal httpx.Client stand-in: main() only reads the chooser body."""

    def __init__(self, html: str) -> None:
        self._html = html

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, url: str) -> _Response:
        return _Response(self._html)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Chain.__table__.create(engine)
    Store.__table__.create(engine)
    with Session(engine, autoflush=False, future=True) as db:
        chain = Chain(code="spudshed", name="SpudShed", kind="supermarket", scrape_enabled=True)
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
                for ref in ACTIVE_REFS
            ]
        )
        db.commit()
        yield db


def _active_refs(session: Session) -> set[str]:
    return set(session.scalars(select(Store.external_ref).where(Store.is_active.is_(True))))


def _card(
    slug: str | None,
    name: str = "Bentley Plaza",
    suburb: str = "Bentley",
    postcode: str = "6102",
) -> dict:
    return {
        "name": name,
        "address": "1 Albany Hwy",
        "suburb": suburb,
        "postcode": postcode,
        "slug": slug,
        "coords": (-32.0, 115.9),
    }


def _stub_page(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    monkeypatch.setattr(seed_spudshed.httpx, "Client", lambda *a, **k: _Client(html))


def _scope(session: Session):
    @contextmanager
    def _session_scope() -> Iterator[Session]:
        yield session
        session.commit()

    return _session_scope


def test_empty_parse_aborts_and_leaves_every_store_active(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_page(monkeypatch, "")
    monkeypatch.setattr(seed_spudshed, "init_db", lambda: None)
    monkeypatch.setattr(seed_spudshed, "session_scope", _scope(session))

    assert seed_spudshed.main([]) == 1
    assert _active_refs(session) == ACTIVE_REFS
    assert "aborting before touching store rows" in capsys.readouterr().out


def test_empty_parse_never_opens_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_page(monkeypatch, "")

    def _unexpected_database_access(*args: object, **kwargs: object) -> None:
        raise AssertionError("database was touched before the parse guard")

    monkeypatch.setattr(seed_spudshed, "init_db", _unexpected_database_access)
    monkeypatch.setattr(seed_spudshed, "session_scope", _unexpected_database_access)

    assert seed_spudshed.main([]) == 1


def test_a_single_parsed_card_is_not_enough_to_retire_rows(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert len(seed_spudshed.parse_cards(ONE_CARD)) == 1
    _stub_page(monkeypatch, ONE_CARD)
    monkeypatch.setattr(seed_spudshed, "init_db", lambda: None)
    monkeypatch.setattr(seed_spudshed, "session_scope", _scope(session))
    monkeypatch.setattr(seed_spudshed, "resolve_subdomain", lambda name, suburb: None)
    monkeypatch.setattr(seed_spudshed, "geocode", lambda *args: None)

    assert seed_spudshed.main([]) == 1
    assert _active_refs(session) == ACTIVE_REFS


def test_retires_stale_rows_when_every_card_resolved(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    chain = session.scalar(select(Chain).where(Chain.code == "spudshed"))
    armadale = session.scalar(
        select(Store).where(Store.external_ref == "spudshed:armadale")
    )
    armadale.is_active = False
    session.commit()

    deactivated = seed_spudshed.sync_stores(session, chain, [_card("bentleyplaza")])
    session.commit()

    # Armadale was already inactive, so it is not counted as deactivated again.
    assert deactivated == len(ACTIVE_REFS) - 2
    assert _active_refs(session) == {"spudshed:bentleyplaza"}
    assert f"deactivated {deactivated} stale rows" in capsys.readouterr().out


def test_upserts_a_store_the_chooser_added(session: Session) -> None:
    chain = session.scalar(select(Chain).where(Chain.code == "spudshed"))

    seed_spudshed.sync_stores(
        session, chain, [_card("carousel", name="Carousel", suburb="Cannington", postcode="6107")]
    )
    session.commit()

    store = session.scalar(
        select(Store).where(Store.external_ref == "spudshed:carousel")
    )
    assert store is not None
    assert store.name == "SpudShed Carousel"
    assert store.suburb == "Cannington"
    assert store.website == "https://carousel.spudshed.com.au"
    assert store.is_active is True


def test_probe_failure_does_not_retire_any_row(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    chain = session.scalar(select(Chain).where(Chain.code == "spudshed"))
    cards = [
        _card("bentleyplaza"),
        _card(None, name="Waterford", suburb="Waterford", postcode="6152"),
    ]

    deactivated = seed_spudshed.sync_stores(session, chain, cards)
    session.commit()

    assert deactivated == 0
    assert _active_refs(session) == ACTIVE_REFS
    assert "not retiring any row because the live list is incomplete" in capsys.readouterr().out
