"""Wellfound adapter: external Apply is supported; the in-app flow is not.

Wellfound's in-app apply control starts a multi-step application hosted
entirely on wellfound.com, with no external ATS page for the rest of the
graph to drive — the same shape of problem as LinkedIn's Easy Apply. It is
detected and skipped first, before any other selector is queried, so the
flow is never entered.
"""

from __future__ import annotations

from typing import Any

from app.boards.base import (
    ApplyResult,
    ApplyStatus,
    BaseBoardAdapter,
    SelectorMap,
    SkipReason,
    click_selector,
    load_selector_map,
    selector_present,
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
        if await selector_present(page, self.selectors.require("in_app_apply_indicator")):
            return ApplyResult(
                status=ApplyStatus.SKIPPED,
                reason=SkipReason.WELLFOUND_IN_APP_APPLY.value,
            )

        clicked = await click_selector(page, self.selectors.require("apply_button"))
        if not clicked:
            return ApplyResult(
                status=ApplyStatus.FAILED,
                reason="no external apply control was found on this listing",
            )
        return ApplyResult(
            status=ApplyStatus.STARTED,
            reason="clicked the external apply control",
            clicked="apply_button",
        )
