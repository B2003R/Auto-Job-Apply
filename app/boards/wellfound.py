"""Wellfound adapter: external Apply is supported; the in-app flow is not.

Wellfound's in-app apply control starts a multi-step application hosted
entirely on wellfound.com, with no external ATS page for the rest of the
graph to drive — the same shape of problem as LinkedIn's Easy Apply. It is
detected and skipped first, before any other selector is queried, so the
flow is never entered. If that detection itself cannot be completed (the
probe raised), the safe response is to fail rather than assume the in-app
flow is absent and click through to it.
"""

from __future__ import annotations

from typing import Any

from app.boards.base import (
    ApplyResult,
    ApplyStatus,
    BaseBoardAdapter,
    ProbeOutcome,
    SelectorMap,
    SkipReason,
    attempt_click,
    load_selector_map,
    probe_selector,
)
from app.storage.models import Board

REQUIRED_SELECTORS: tuple[str, ...] = ("in_app_apply_indicator", "apply_button")


class WellfoundAdapter(BaseBoardAdapter):
    board = Board.WELLFOUND

    def __init__(self, selectors: SelectorMap | None = None) -> None:
        super().__init__(
            selectors
            if selectors is not None
            else load_selector_map(Board.WELLFOUND, required=REQUIRED_SELECTORS)
        )

    async def start_application(self, page: Any) -> ApplyResult:
        in_app = await probe_selector(page, self.selectors.require("in_app_apply_indicator"))
        if in_app.outcome is ProbeOutcome.INDETERMINATE:
            return ApplyResult(
                status=ApplyStatus.FAILED,
                reason=(
                    "could not determine whether this listing uses the in-app "
                    f"apply flow ({in_app.detail}); refusing to guess and click "
                    "the external apply control instead"
                ),
            )
        if in_app.present:
            return ApplyResult(
                status=ApplyStatus.SKIPPED,
                reason=SkipReason.WELLFOUND_IN_APP_APPLY.value,
            )

        attempt = await attempt_click(page, self.selectors.require("apply_button"))
        if not attempt.clicked:
            return ApplyResult(
                status=ApplyStatus.FAILED,
                reason=f"could not click the external apply control: {attempt.detail}",
            )
        return ApplyResult(
            status=ApplyStatus.STARTED,
            reason="clicked the external apply control",
            clicked="apply_button",
        )
