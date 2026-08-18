"""Tests for persisted, per-board, UTC-day rate caps.

The cap is a safety control, not a performance knob: exceeding it is how an
account gets flagged. So the properties under test are the ones that make it
impossible to exceed by accident — the count is persisted (a restart does not
reset it), the window is the UTC day (a local midnight does not reset it),
there is no runtime bypass parameter, and the check and the record happen in
one atomic step so two concurrent workers cannot both see "39 of 40".
"""

from __future__ import annotations

import inspect
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agent.errors import RateLimitExceeded
from app.agent.rate_limiter import RateDecision, RateLimiter
from app.config import Settings
from app.storage.db import Database
from app.storage.models import Board

DAY = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class FrozenClock:
    """A movable UTC clock, so day boundaries are tested without waiting."""

    def __init__(self, now: datetime = DAY) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        sqlite_path=tmp_path / "rates.db",
        artifacts_path=tmp_path / "artifacts",
        linkedin_daily_cap=3,
        jobright_daily_cap=2,
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings)
    database.initialize()
    return database


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def limiter(db: Database, settings: Settings, clock: FrozenClock) -> RateLimiter:
    return RateLimiter(db, settings, clock=clock)


class TestCapEnforcement:
    def test_records_each_action_until_the_cap_is_reached(self, limiter: RateLimiter) -> None:
        decisions = [
            limiter.check_and_record(Board.LINKEDIN, "apply") for _ in range(3)
        ]

        assert [decision.count for decision in decisions] == [1, 2, 3]
        assert all(decision.cap == 3 for decision in decisions)
        assert decisions[-1].remaining == 0

    def test_the_call_past_the_cap_raises(self, limiter: RateLimiter) -> None:
        for _ in range(3):
            limiter.check_and_record(Board.LINKEDIN, "apply")

        with pytest.raises(RateLimitExceeded) as excinfo:
            limiter.check_and_record(Board.LINKEDIN, "apply")

        error = excinfo.value
        assert error.board is Board.LINKEDIN
        assert error.cap == 3
        assert error.count == 3
        assert "linkedin" in str(error)
        assert "3" in str(error)
        assert error.next_reset_at.isoformat() in str(error)

    def test_a_refused_call_records_nothing(
        self, limiter: RateLimiter, db: Database
    ) -> None:
        for _ in range(3):
            limiter.check_and_record(Board.LINKEDIN, "apply")

        for _ in range(5):
            with pytest.raises(RateLimitExceeded):
                limiter.check_and_record(Board.LINKEDIN, "apply")

        assert db.count_rate_events(Board.LINKEDIN, DAY.date()) == 3

    def test_caps_are_counted_per_board(self, limiter: RateLimiter) -> None:
        for _ in range(3):
            limiter.check_and_record(Board.LINKEDIN, "apply")

        jobright = limiter.check_and_record(Board.JOBRIGHT, "apply")

        assert jobright.count == 1
        assert limiter.remaining(Board.JOBRIGHT) == 1
        assert limiter.remaining(Board.LINKEDIN) == 0

    def test_every_action_on_a_board_counts_towards_the_same_cap(
        self, limiter: RateLimiter
    ) -> None:
        limiter.check_and_record(Board.JOBRIGHT, "apply")
        limiter.check_and_record(Board.JOBRIGHT, "open_listing")

        with pytest.raises(RateLimitExceeded):
            limiter.check_and_record(Board.JOBRIGHT, "apply")

    def test_a_zero_cap_blocks_the_first_action(
        self, db: Database, clock: FrozenClock, tmp_path: Path
    ) -> None:
        settings = Settings(
            _env_file=None,
            sqlite_path=tmp_path / "rates.db",
            wellfound_daily_cap=0,
        )
        limiter = RateLimiter(db, settings, clock=clock)

        with pytest.raises(RateLimitExceeded):
            limiter.check_and_record(Board.WELLFOUND, "apply")

        assert db.count_rate_events(Board.WELLFOUND, DAY.date()) == 0

    def test_a_negative_cap_is_treated_as_zero_not_unlimited(
        self, db: Database, clock: FrozenClock, tmp_path: Path
    ) -> None:
        settings = Settings(
            _env_file=None,
            sqlite_path=tmp_path / "rates.db",
            handshake_daily_cap=-5,
        )
        limiter = RateLimiter(db, settings, clock=clock)

        assert limiter.cap_for(Board.HANDSHAKE) == 0
        with pytest.raises(RateLimitExceeded):
            limiter.check_and_record(Board.HANDSHAKE, "apply")

    def test_every_board_has_a_configured_cap(self, limiter: RateLimiter) -> None:
        assert {board: limiter.cap_for(board) for board in Board} == {
            Board.LINKEDIN: 3,
            Board.JOBRIGHT: 2,
            Board.WELLFOUND: 40,
            Board.HANDSHAKE: 40,
        }


class TestUtcWindow:
    def test_the_next_utc_day_resets_the_count(
        self, limiter: RateLimiter, clock: FrozenClock
    ) -> None:
        for _ in range(3):
            limiter.check_and_record(Board.LINKEDIN, "apply")

        clock.now = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
        decision = limiter.check_and_record(Board.LINKEDIN, "apply")

        assert decision.count == 1
        assert decision.utc_day == clock.now.date()

    def test_later_the_same_utc_day_does_not_reset(
        self, limiter: RateLimiter, clock: FrozenClock
    ) -> None:
        limiter.check_and_record(Board.LINKEDIN, "apply")
        clock.advance(hours=11, minutes=59)

        decision = limiter.check_and_record(Board.LINKEDIN, "apply")

        assert decision.count == 2

    def test_local_midnight_in_another_zone_does_not_reset(
        self, limiter: RateLimiter, clock: FrozenClock
    ) -> None:
        clock.now = datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc)
        limiter.check_and_record(Board.LINKEDIN, "apply")

        # 2026-08-19T00:30 in UTC+05:00 is still 2026-08-18 in UTC.
        clock.now = datetime(
            2026, 8, 19, 0, 30, tzinfo=timezone(timedelta(hours=5))
        )
        decision = limiter.check_and_record(Board.LINKEDIN, "apply")

        assert decision.utc_day == datetime(2026, 8, 18).date()
        assert decision.count == 2

    def test_a_naive_clock_reading_is_interpreted_as_utc(
        self, limiter: RateLimiter, clock: FrozenClock
    ) -> None:
        clock.now = datetime(2026, 8, 18, 23, 0)

        decision = limiter.check_and_record(Board.LINKEDIN, "apply")

        assert decision.utc_day == datetime(2026, 8, 18).date()

    def test_the_reset_time_is_the_next_utc_midnight(self, limiter: RateLimiter) -> None:
        decision = limiter.check_and_record(Board.LINKEDIN, "apply")

        assert decision.next_reset_at == datetime(
            2026, 8, 19, 0, 0, tzinfo=timezone.utc
        )

    def test_yesterdays_events_do_not_count_today(
        self, db: Database, limiter: RateLimiter, clock: FrozenClock
    ) -> None:
        for hour in range(5):
            db.record_rate_event(
                Board.LINKEDIN,
                "apply",
                datetime(2026, 8, 17, hour, tzinfo=timezone.utc),
            )

        assert limiter.remaining(Board.LINKEDIN) == 3
        assert limiter.check_and_record(Board.LINKEDIN, "apply").count == 1


class TestPersistence:
    def test_counts_survive_a_new_limiter_and_database(
        self, settings: Settings, db: Database, clock: FrozenClock
    ) -> None:
        first = RateLimiter(db, settings, clock=clock)
        first.check_and_record(Board.LINKEDIN, "apply")
        first.check_and_record(Board.LINKEDIN, "apply")

        reopened = Database(settings)
        reopened.initialize()
        second = RateLimiter(reopened, settings, clock=clock)

        assert second.remaining(Board.LINKEDIN) == 1
        second.check_and_record(Board.LINKEDIN, "apply")
        with pytest.raises(RateLimitExceeded):
            second.check_and_record(Board.LINKEDIN, "apply")

    def test_recorded_events_carry_the_board_action_and_utc_timestamp(
        self, limiter: RateLimiter, db: Database
    ) -> None:
        limiter.check_and_record(Board.LINKEDIN, "apply")

        with db.connect() as conn:
            rows = conn.execute("SELECT board, action, timestamp FROM rate_events").fetchall()

        assert len(rows) == 1
        assert rows[0]["board"] == "linkedin"
        assert rows[0]["action"] == "apply"
        assert datetime.fromisoformat(rows[0]["timestamp"]) == DAY


class TestNoOverridePath:
    def test_check_and_record_takes_only_a_board_and_an_action(self) -> None:
        parameters = list(inspect.signature(RateLimiter.check_and_record).parameters)

        assert parameters == ["self", "board", "action"]

    def test_no_bypass_shaped_api_is_exposed(self) -> None:
        public = {name for name in dir(RateLimiter) if not name.startswith("_")}
        forbidden_names = {
            "force",
            "override",
            "bypass",
            "reset",
            "reset_counts",
            "clear",
            "clear_events",
            "set_cap",
            "disable",
            "record",
        }

        assert not (public & forbidden_names), sorted(public)

    def test_no_public_method_accepts_a_cap_or_a_bypass_argument(self) -> None:
        forbidden_parameters = {"cap", "caps", "force", "override", "bypass", "limit"}

        for name in dir(RateLimiter):
            member = getattr(RateLimiter, name)
            if name.startswith("_") or not callable(member):
                continue
            parameters = set(inspect.signature(member).parameters)
            assert not (parameters & forbidden_parameters), name

    def test_the_cap_comes_from_settings_only(
        self, db: Database, settings: Settings, clock: FrozenClock
    ) -> None:
        limiter = RateLimiter(db, settings, clock=clock)

        assert limiter.cap_for(Board.LINKEDIN) == settings.linkedin_daily_cap

    def test_decisions_are_immutable(self, limiter: RateLimiter) -> None:
        decision = limiter.check_and_record(Board.LINKEDIN, "apply")

        assert isinstance(decision, RateDecision)
        with pytest.raises(Exception):
            decision.count = 0  # type: ignore[misc]


class TestConcurrency:
    def test_concurrent_workers_never_exceed_the_cap(
        self, db: Database, settings: Settings, clock: FrozenClock
    ) -> None:
        cap = settings.linkedin_daily_cap
        workers = 24
        start = threading.Barrier(workers)
        granted: list[RateDecision] = []
        refused: list[RateLimitExceeded] = []
        errors: list[BaseException] = []
        guard = threading.Lock()

        def attempt() -> None:
            limiter = RateLimiter(db, settings, clock=clock)
            start.wait(timeout=10)
            try:
                decision = limiter.check_and_record(Board.LINKEDIN, "apply")
            except RateLimitExceeded as exc:
                with guard:
                    refused.append(exc)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions
                with guard:
                    errors.append(exc)
            else:
                with guard:
                    granted.append(decision)

        threads = [threading.Thread(target=attempt) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        assert len(granted) == cap
        assert len(refused) == workers - cap
        assert sorted(decision.count for decision in granted) == list(range(1, cap + 1))
        assert db.count_rate_events(Board.LINKEDIN, DAY.date()) == cap

    def test_the_check_and_the_record_share_one_transaction(
        self, db: Database, settings: Settings, clock: FrozenClock
    ) -> None:
        """A second connection must not observe a half-applied admission."""
        limiter = RateLimiter(db, settings, clock=clock)
        limiter.check_and_record(Board.LINKEDIN, "apply")

        with sqlite3.connect(settings.sqlite_path) as observer:
            count = observer.execute("SELECT COUNT(*) FROM rate_events").fetchone()[0]

        assert count == 1


class TestAtomicStorage:
    def test_the_database_refuses_to_insert_past_the_cap(self, db: Database) -> None:
        recorded_first, count_first = db.try_record_rate_event(
            Board.LINKEDIN, "apply", DAY, cap=1
        )
        recorded_second, count_second = db.try_record_rate_event(
            Board.LINKEDIN, "apply", DAY, cap=1
        )

        assert (recorded_first, count_first) == (True, 1)
        assert (recorded_second, count_second) == (False, 1)
        assert db.count_rate_events(Board.LINKEDIN, DAY.date()) == 1

    def test_the_cap_check_is_scoped_to_the_boards_own_utc_day(
        self, db: Database
    ) -> None:
        db.record_rate_event(Board.JOBRIGHT, "apply", DAY)
        db.record_rate_event(
            Board.LINKEDIN, "apply", DAY - timedelta(days=1)
        )

        recorded, count = db.try_record_rate_event(Board.LINKEDIN, "apply", DAY, cap=1)

        assert (recorded, count) == (True, 1)
