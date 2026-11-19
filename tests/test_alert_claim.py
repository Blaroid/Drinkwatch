"""One alert must not queue two notifications when evaluations overlap.

The 30-minute timer, the post-scrape call and POST /v1/admin/alerts/evaluate can
all be inside evaluate_alerts() at once. Before the claim update both runs read
the same last_notified_at, both matched the same price and both inserted a
notification -- reproduced 10/10 with two concurrent calls (audit #34 item 13).

SQLite stands in for Postgres here. The statement under test is a portable
UPDATE ... RETURNING, and the property that stops the duplicate is the second
session seeing the first session's committed claim. bulk_latest_prices() is
replaced because its DISTINCT ON query is Postgres-only; the price read is
covered by its own tests.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import notify
from app.models import Chain, Device, Notification, Observation, PriceAlert, Product, Store
from app.notify import QUIET_PERIOD, _claim_alert, evaluate_alerts

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
_KEYS = itertools.count()


@pytest.fixture()
def engine(tmp_path):  # noqa: ANN001, ANN201 - sqlalchemy Engine
    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    # notifications is created by hand because its payload column is JSONB,
    # which SQLite cannot render; a text column takes the same JSON.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table notifications ("
            " id integer primary key autoincrement,"
            " device_id integer not null,"
            " kind varchar(20) not null default 'alert',"
            " title varchar(200) not null,"
            " body text not null,"
            " payload text not null default '{}',"
            " read_at timestamp,"
            " created_at timestamp default current_timestamp)"
        )
    for table in (
        Chain.__table__,
        Store.__table__,
        Device.__table__,
        Product.__table__,
        Observation.__table__,
        PriceAlert.__table__,
    ):
        table.create(engine)
    return engine


def _session(engine) -> Session:  # noqa: ANN001 - sqlalchemy Engine
    return Session(engine, autoflush=False, expire_on_commit=False, future=True)


def _alert_row(session: Session, last_notified_at: datetime | None = None) -> int:
    """One device, product, chain, store and matching crowd sighting, plus the alert."""
    key = next(_KEYS)
    device = Device(token_hash=f"device-{key}")
    product = Product(slug=f"monster-ultra-{key}", brand="Monster", name="Ultra")
    chain = Chain(code=f"coles-{key}", name="Coles", kind="supermarket")
    session.add_all([device, product, chain])
    session.flush()
    store = Store(
        chain_id=chain.id, name="Coles Perth", external_ref=f"store-{key}", is_active=True
    )
    session.add(store)
    session.flush()
    session.add(
        Observation(
            # SQLite will not fill in a BigInteger primary key, and another test
            # module's mapper hook must not be what makes this work here.
            id=key + 1,
            store_id=store.id,
            chain_id=chain.id,
            product_id=product.id,
            price_cents=200,
            source="crowd",
            price_scope="store",
            status="accepted",
            observed_at=NOTIFY_NOW,
        )
    )
    alert = PriceAlert(
        device_id=device.id,
        product_id=product.id,
        max_price_cents=250,
        last_notified_at=last_notified_at,
    )
    session.add(alert)
    session.commit()
    return alert.id


# evaluate_alerts() stamps its own "now"; keep the seeded sighting inside its
# freshness window rather than depending on the wall clock at test time.
NOTIFY_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _stub_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    def prices(session, stores, product_ids, since):  # noqa: ANN001, ANN202 - test double
        return {
            (store.id, product_id): {
                "price_cents": 200,
                "promo_kind": "none",
                "price_scope": "store",
            }
            for store in stores
            for product_id in product_ids
        }

    monkeypatch.setattr(notify, "bulk_latest_prices", prices)


# ---------- the claim itself ----------


def test_second_session_cannot_claim_an_alert_the_first_already_notified(
    engine,  # noqa: ANN001
) -> None:
    with _session(engine) as setup:
        alert_id = _alert_row(setup)

    with _session(engine) as first, _session(engine) as second:
        # Both runs load the same stale row before either claims it: this is the
        # state the old quiet-period check acted on.
        assert first.get(PriceAlert, alert_id).last_notified_at is None
        assert second.get(PriceAlert, alert_id).last_notified_at is None

        assert _claim_alert(first, alert_id, NOW) is True
        first.commit()

        assert _claim_alert(second, alert_id, NOW) is False


def test_claim_skips_an_alert_inside_its_quiet_period(engine) -> None:  # noqa: ANN001
    with _session(engine) as session:
        alert_id = _alert_row(session, last_notified_at=NOW - timedelta(hours=1))
        assert _claim_alert(session, alert_id, NOW) is False


def test_claim_takes_an_alert_whose_quiet_period_has_passed(engine) -> None:  # noqa: ANN001
    with _session(engine) as session:
        alert_id = _alert_row(session, last_notified_at=NOW - QUIET_PERIOD - timedelta(minutes=1))
        assert _claim_alert(session, alert_id, NOW) is True
        session.commit()
        assert session.get(PriceAlert, alert_id).last_notified_at is not None


def test_claim_of_an_unknown_alert_reports_nothing(engine) -> None:  # noqa: ANN001
    with _session(engine) as session:
        assert _claim_alert(session, 999, NOW) is False


# ---------- end to end ----------


def test_overlapping_evaluations_queue_one_notification(
    engine,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Evidence case: two evaluations that both loaded the alert before either
    # wrote anything. The second must find nothing left to claim.
    _stub_prices(monkeypatch)
    with _session(engine) as setup:
        alert_id = _alert_row(setup)

    with _session(engine) as first, _session(engine) as second:
        assert first.get(PriceAlert, alert_id).last_notified_at is None
        assert second.get(PriceAlert, alert_id).last_notified_at is None

        assert evaluate_alerts(first) == 1
        assert evaluate_alerts(second) == 0

    with _session(engine) as check:
        assert check.scalar(select(func.count()).select_from(Notification)) == 1


def test_a_failed_claim_skips_only_that_alert(
    engine,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A claim lost to another run must not abort the batch: the remaining alerts
    # still deserve their notification, and the lost one must be left unclaimed.
    _stub_prices(monkeypatch)
    with _session(engine) as setup:
        lost = _alert_row(setup)
        kept = _alert_row(setup)

    real_claim = notify._claim_alert

    def claim(session, alert_id, now):  # noqa: ANN001, ANN202 - test double
        if alert_id == lost:
            return False
        return real_claim(session, alert_id, now)

    monkeypatch.setattr(notify, "_claim_alert", claim)

    with _session(engine) as session:
        assert evaluate_alerts(session) == 1

    with _session(engine) as check:
        assert check.get(PriceAlert, lost).last_notified_at is None
        assert check.get(PriceAlert, kept).last_notified_at is not None
        assert check.scalar(select(func.count()).select_from(Notification)) == 1


def test_evaluation_claims_the_alert_it_notified(
    engine,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_prices(monkeypatch)
    with _session(engine) as session:
        alert_id = _alert_row(session)
        assert evaluate_alerts(session) == 1

    # A fresh session: the evaluator deliberately does not sync the in-memory
    # copy, the committed row is the source of truth.
    with _session(engine) as check:
        assert check.get(PriceAlert, alert_id).last_notified_at is not None


def test_a_second_run_after_the_quiet_period_notifies_again(engine) -> None:  # noqa: ANN001
    # The claim must not turn into a one-shot: an alert still matching a day
    # later has to fire again, which is the whole point of the 24h period.
    with _session(engine) as session:
        alert_id = _alert_row(session)
        assert _claim_alert(session, alert_id, NOW) is True
        session.commit()
        assert _claim_alert(session, alert_id, NOW + QUIET_PERIOD + timedelta(minutes=1)) is True
