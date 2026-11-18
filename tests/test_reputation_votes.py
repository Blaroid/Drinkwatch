"""Reputation and votes must change an outcome, not just decorate a row.

Pure unit tests: no database, no network. They pin the three decisions from the
review item — a confirm from a trusted device counts twice, a tie goes to the
confirmed row and then the more reputable author, and a row the crowd disputed
is not a candidate for the headline price at all. The SQL itself is only held
by its text here; there is no Postgres in this test run, so the query is
checked for the vote join and the dispute filter, not executed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast

from app.api.crowd import _needs_review, _status_after_votes, vote
from app.api.deps import (
    DISPUTE_LIMIT,
    EXEMPT_REPUTATION,
    TRUSTED_CONFIRM_WEIGHT,
    TRUSTED_REPUTATION,
    _CHAIN_SCOPE_SQL,
    _STORE_SCOPE_SQL,
    VoteSupport,
    _freshest,
    confirm_weight,
    latest_prices,
)
from app.models import Device, Observation, Store
from app.schemas import VoteIn

NOW = datetime(2026, 11, 18, 5, 20, tzinfo=timezone.utc)
CHAIN_ID = 1
STORE_ID = 15


def _obs(
    *,
    price_cents: int,
    hours_ago: float = 1.0,
    store_id: int | None = STORE_ID,
    source: str = "crowd",
    scope: str = "store",
    obs_id: int = 1,
) -> Observation:
    return Observation(
        id=obs_id,
        store_id=store_id,
        chain_id=CHAIN_ID,
        product_id=7,
        price_cents=price_cents,
        promo_kind="none",
        source=source,
        price_scope=scope,
        observed_at=NOW - timedelta(hours=hours_ago),
    )


def test_a_confirm_from_a_trusted_device_is_worth_two() -> None:
    assert confirm_weight(TRUSTED_REPUTATION) == TRUSTED_CONFIRM_WEIGHT
    assert confirm_weight(TRUSTED_REPUTATION - 1) == 1
    # A row from a scraper has no author, and a brand-new device has no history.
    assert confirm_weight(None) == 1
    assert confirm_weight(0) == 1


def test_one_trusted_confirm_clears_the_review_queue() -> None:
    trusted = confirm_weight(TRUSTED_REPUTATION)
    assert _status_after_votes("needs_review", trusted, 0) == "accepted"
    # Untrusted: one confirm is still not the two the queue asks for.
    assert _status_after_votes("needs_review", confirm_weight(TRUSTED_REPUTATION - 1), 0) == (
        "needs_review"
    )
    assert _status_after_votes("needs_review", 2, 0) == "accepted"


def test_disputes_take_an_accepted_row_out_of_play() -> None:
    assert _status_after_votes("accepted", 5, DISPUTE_LIMIT - 1) == "accepted"
    assert _status_after_votes("accepted", 5, DISPUTE_LIMIT) == "needs_review"
    # Disputes are applied after confirms, so arguing does not sneak a row back in.
    assert _status_after_votes("needs_review", 2, DISPUTE_LIMIT) == "needs_review"


def test_exempt_reputation_skips_the_review_queue() -> None:
    assert _needs_review(900, 300, EXEMPT_REPUTATION) is False
    # One reputation point short and the outlier check still applies.
    assert _needs_review(900, 300, EXEMPT_REPUTATION - 1) is True
    assert _needs_review(300, 300, 0) is False
    # No reference price means no outlier test to fail.
    assert _needs_review(300, None, 0) is False


def test_a_tied_row_with_more_weighted_confirms_wins_the_tie() -> None:
    store_row = _obs(price_cents=315, hours_ago=2.0, obs_id=14)
    chain_row = _obs(
        price_cents=350, hours_ago=2.0, store_id=None, source="scrape", scope="chain", obs_id=15
    )
    support = {
        store_row.id: VoteSupport(confirm_weight=1),
        chain_row.id: VoteSupport(confirm_weight=2),
    }

    chosen = _freshest(store_row, chain_row, STORE_ID, support=support)

    assert chosen is not None
    assert chosen.primary.price_cents == 350
    assert chosen.fallback is not None and chosen.fallback.price_cents == 315


def test_equal_confirms_break_on_the_more_reputable_author() -> None:
    store_row = _obs(price_cents=315, hours_ago=2.0, obs_id=20)
    chain_row = _obs(
        price_cents=350, hours_ago=2.0, store_id=None, source="scrape", scope="chain", obs_id=21
    )
    support = {
        store_row.id: VoteSupport(confirm_weight=1, device_reputation=5),
        chain_row.id: VoteSupport(confirm_weight=1, device_reputation=40),
    }

    chosen = _freshest(store_row, chain_row, STORE_ID, support=support)

    assert chosen is not None
    assert chosen.primary.price_cents == 350


def test_a_tie_with_no_votes_still_prefers_the_store_row() -> None:
    """The crowd only decides ties; scope is the fallback, not the rule."""
    store_row = _obs(price_cents=315, hours_ago=2.0, obs_id=22)
    chain_row = _obs(
        price_cents=350, hours_ago=2.0, store_id=None, source="scrape", scope="chain", obs_id=23
    )

    chosen = _freshest(store_row, chain_row, STORE_ID, support={})

    assert chosen is not None
    assert chosen.primary.price_cents == 315


def test_both_scope_queries_exclude_disputed_rows() -> None:
    """No Postgres here, so the 'cannot be the headline' rule is pinned to SQL.

    The filter has to be in the query: a disputed newest row must be skipped
    before DISTINCT ON picks, otherwise the row is still the offer.
    """
    for statement in (_STORE_SCOPE_SQL, _CHAIN_SCOPE_SQL):
        sql = str(statement)
        assert "observation_votes" in sql
        assert "coalesce(votes.dispute_weight, 0) < :dispute_limit" in sql


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict]:
        return self._rows


class _FakeSession:
    """Just enough of Session.execute to run latest_prices()' two queries."""

    def __init__(self, store_rows: list[dict], chain_rows: list[dict]) -> None:
        self.store_rows = store_rows
        self.chain_rows = chain_rows
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement, params):  # noqa: ANN001 - duck-typed test double
        self.calls.append((str(statement), params))
        if "o.store_id is null" in str(statement):
            return _FakeResult(self.chain_rows)
        return _FakeResult(self.store_rows)


class _FakeStore:
    def __init__(self, store_id: int, chain_id: int) -> None:
        self.id = store_id
        self.chain_id = chain_id


def _stores() -> list[Store]:
    # latest_prices() only reads .id and .chain_id; the cast keeps the type
    # checker honest about that duck-typed stand-in.
    return cast(list[Store], [_FakeStore(STORE_ID, CHAIN_ID)])


def _row(
    *,
    obs_id: int,
    price_cents: int,
    store_id: int | None,
    scope: str,
    hours_ago: float,
    confirm_weight: int = 0,
    device_reputation: int = 0,
) -> dict:
    return {
        "id": obs_id,
        "store_id": store_id,
        "chain_id": CHAIN_ID,
        "product_id": 7,
        "price_cents": price_cents,
        "was_price_cents": None,
        "unit_price_cents_per_l": None,
        "promo_kind": "none",
        "promo_qty": None,
        "promo_total_cents": None,
        "in_stock": True,
        "source": "crowd" if store_id is not None else "scrape",
        "price_scope": scope,
        "observed_at": NOW - timedelta(hours=hours_ago),
        "confirm_weight": confirm_weight,
        "device_reputation": device_reputation,
    }


def test_latest_prices_passes_the_reputation_rules_to_the_query() -> None:
    session = _FakeSession(
        [_row(obs_id=10, price_cents=315, store_id=STORE_ID, scope="store", hours_ago=2)],
        [],
    )

    latest_prices(session, _stores(), [7], NOW - timedelta(hours=72))

    assert len(session.calls) == 2
    for _, params in session.calls:
        assert params["trusted_reputation"] == TRUSTED_REPUTATION
        assert params["trusted_confirm_weight"] == TRUSTED_CONFIRM_WEIGHT
        assert params["dispute_limit"] == DISPUTE_LIMIT


def test_vote_columns_are_not_handed_to_the_observation() -> None:
    """The join aliases are query decorations; Observation has no such fields."""
    session = _FakeSession(
        [
            _row(
                obs_id=10,
                price_cents=315,
                store_id=STORE_ID,
                scope="store",
                hours_ago=2,
                confirm_weight=3,
                device_reputation=25,
            )
        ],
        [],
    )

    prices = latest_prices(session, _stores(), [7], NOW - timedelta(hours=72))

    assert prices[STORE_ID].primary.price_cents == 315


def test_a_tied_chain_row_with_more_confirms_becomes_the_offer() -> None:
    session = _FakeSession(
        [
            _row(
                obs_id=10,
                price_cents=315,
                store_id=STORE_ID,
                scope="store",
                hours_ago=2,
                confirm_weight=1,
                device_reputation=1,
            )
        ],
        [
            _row(
                obs_id=11,
                price_cents=350,
                store_id=None,
                scope="chain",
                hours_ago=2,
                confirm_weight=2,
                device_reputation=0,
            )
        ],
    )

    prices = latest_prices(session, _stores(), [7], NOW - timedelta(hours=72))

    assert prices[STORE_ID].primary.price_cents == 350
    assert prices[STORE_ID].primary.price_scope == "chain"
    fallback = prices[STORE_ID].fallback
    assert fallback is not None and fallback.price_cents == 315


class _CountsRow:
    def __init__(self, observation_id: int, confirms: int, disputes: int) -> None:
        self.observation_id = observation_id
        self.confirms = confirms
        self.disputes = disputes


class _VoteSession:
    """Just enough of Session for crowd.vote(): auth lookup, tally, serialize.

    `tally` is what the reputation-weighted vote query returns, as
    (vote_value, voter_reputation) pairs.
    """

    def __init__(
        self, observation: Observation, voter: Device, tally: list[tuple[int, int]]
    ) -> None:
        self.observation = observation
        self.voter = voter
        self.tally = tally
        self.flushed = False
        self.added: list[object] = []

    def scalar(self, statement):  # noqa: ANN001 - duck-typed test double
        return self.voter

    def get(self, entity, ident):  # noqa: ANN001
        if entity is Observation:
            return self.observation
        return None  # this device has not voted on the row yet

    def add(self, instance):  # noqa: ANN001
        self.added.append(instance)

    def flush(self) -> None:
        self.flushed = True

    def commit(self) -> None:
        pass

    def execute(self, statement):  # noqa: ANN001
        if "greatest(" in str(statement):
            return [_CountsRow(self.observation.id, 0, 0)]
        return self.tally


def _device(device_id: int, reputation: int) -> Device:
    return Device(
        id=device_id,
        token_hash="irrelevant",
        display_name=None,
        platform=None,
        app_version=None,
        reputation=reputation,
        is_banned=False,
    )


def _vote_row(status: str, obs_id: int, author_id: int = 99) -> Observation:
    observation = _obs(price_cents=315, hours_ago=1.0, obs_id=obs_id)
    observation.status = status
    observation.device_id = author_id
    return observation


def test_a_trusted_confirm_clears_the_review_queue_in_one_call() -> None:
    observation = _vote_row("needs_review", obs_id=30)
    voter = _device(device_id=5, reputation=TRUSTED_REPUTATION)
    session = _VoteSession(observation, voter, [(1, TRUSTED_REPUTATION)])

    result = vote(
        observation.id, VoteIn(vote=1), authorization="Bearer test-token", session=session
    )

    # The vote just cast is pending; without the flush the tally is one behind
    # and this confirm would not count until a later vote.
    assert session.flushed
    assert result.status == "accepted"


def test_an_ordinary_confirm_still_leaves_the_row_in_review() -> None:
    observation = _vote_row("needs_review", obs_id=31)
    voter = _device(device_id=6, reputation=TRUSTED_REPUTATION - 1)
    session = _VoteSession(observation, voter, [(1, TRUSTED_REPUTATION - 1)])

    result = vote(
        observation.id, VoteIn(vote=1), authorization="Bearer test-token", session=session
    )

    assert result.status == "needs_review"


def test_three_disputes_take_an_accepted_row_out_even_from_reputable_voters() -> None:
    observation = _vote_row("accepted", obs_id=32)
    voter = _device(device_id=7, reputation=80)
    session = _VoteSession(observation, voter, [(-1, 80), (-1, 80), (-1, 80)])

    result = vote(
        observation.id, VoteIn(vote=-1), authorization="Bearer test-token", session=session
    )

    assert result.status == "needs_review"
