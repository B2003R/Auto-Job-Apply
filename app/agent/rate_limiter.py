"""Per-board daily action caps, counted in UTC and persisted in SQLite.

The cap exists because volume is what gets an account flagged, so it is
enforced as a hard precondition rather than advice:

* **Persisted, not in-memory.** The count lives in `rate_events`, so a
  restarted process, a second worker, or a crashed run resumes with the
  day's history intact instead of a fresh budget.
* **UTC, not local.** A single fixed day boundary means the reset time is the
  same regardless of where the machine is or whether it moved; a local
  midnight (or a DST shift) never grants extra actions.
* **One atomic check-and-record.** `check_and_record` performs the count and
  the insert inside one immediate transaction, so two concurrent workers
  cannot both read "39 of 40" and both proceed.
* **No override path.** The only inputs are the board and the action; the cap
  comes from `Settings`, which comes from the environment. There is no
  `force=`, no "just this once", and nothing here deletes a recorded event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Mapping

from app.agent.errors import RateLimitExceeded
from app.config import Settings
from app.storage.db import Database
from app.storage.models import Board

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RateDecision:
    """A granted action, with the budget it consumed."""

    board: Board
    action: str
    count: int
    cap: int
    utc_day: date
    next_reset_at: datetime

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.count)


class RateLimiter:
    """Counts and admits per-board actions against a persisted daily cap."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._db = db
        self._settings = settings
        self._clock = clock

    def cap_for(self, board: Board) -> int:
        """The configured cap for a board; never negative."""
        return max(0, int(self._caps()[board]))

    def count(self, board: Board) -> int:
        """Actions already recorded for this board on the current UTC day."""
        return self._db.count_rate_events(board, self._utc_day())

    def remaining(self, board: Board) -> int:
        return max(0, self.cap_for(board) - self.count(board))

    def next_reset_at(self) -> datetime:
        """The next UTC midnight, when every board's count returns to zero."""
        return datetime.combine(
            self._utc_day() + timedelta(days=1), time.min, tzinfo=timezone.utc
        )

    def check_and_record(self, board: Board, action: str) -> RateDecision:
        """Admit and record one action, or raise `RateLimitExceeded`.

        The check and the record are one transaction, so the returned
        `count` is the caller's own position in the day's budget rather than
        a number that may already be stale. Nothing is recorded when the
        call is refused.
        """
        moment = self._now()
        cap = self.cap_for(board)
        recorded, count = self._db.try_record_rate_event(board, action, moment, cap)
        if not recorded:
            raise RateLimitExceeded(
                board=board,
                cap=cap,
                count=count,
                next_reset_at=self._next_reset_from(moment),
            )
        return RateDecision(
            board=board,
            action=action,
            count=count,
            cap=cap,
            utc_day=moment.date(),
            next_reset_at=self._next_reset_from(moment),
        )

    def _caps(self) -> Mapping[Board, int]:
        """Read caps from settings on every call.

        Nothing is cached, so there is no in-memory copy for a caller to
        reach in and edit; changing a cap means changing the environment and
        restarting.
        """
        return {
            Board.LINKEDIN: self._settings.linkedin_daily_cap,
            Board.JOBRIGHT: self._settings.jobright_daily_cap,
            Board.WELLFOUND: self._settings.wellfound_daily_cap,
            Board.HANDSHAKE: self._settings.handshake_daily_cap,
        }

    def _now(self) -> datetime:
        """The clock's reading, normalized to UTC.

        A naive reading is interpreted as UTC rather than as local time: the
        storage layer makes the same assumption, and guessing local time
        would silently shift the day boundary on a non-UTC machine.
        """
        moment = self._clock()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _utc_day(self) -> date:
        return self._now().date()

    @staticmethod
    def _next_reset_from(moment: datetime) -> datetime:
        return datetime.combine(
            moment.date() + timedelta(days=1), time.min, tzinfo=timezone.utc
        )
