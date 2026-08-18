"""Jobright adapter: direct Apply-with-Autofill, plus normal Apply.

Jobright's own listing page can offer an explicit "Apply with Autofill"
control that starts the extension's autofill flow immediately, without
first routing through a plain Apply button. That control is looked for and
clicked *first*, but only when the selector map names one explicitly and it
resolves to exactly one visible element on the page (see
`app.boards.base.click_selector`); an absent, unconfigured, or ambiguous
match falls through to the normal Apply control rather than guessing which
of several similar-looking buttons is the real one.

This adapter is distinct from the four-tier `JobrightTrigger` in
`app.agent.jobright_trigger`: that trigger fires *after* an ATS page has
already loaded, following either path started here. This adapter only gets
the application started from Jobright's own listing page.
"""

from __future__ import annotations

from typing import Any

from app.boards.base import (
    ApplyResult,
    ApplyStatus,
    BaseBoardAdapter,
    SelectorMap,
    click_selector,
    load_selector_map,
)
from app.storage.models import Board

#: `autofill_apply_button` is deliberately not required: Jobright's normal
#: apply flow must keep working even when that control is not configured.
REQUIRED_SELECTORS: tuple[str, ...] = ("apply_button",)


class JobrightAdapter(BaseBoardAdapter):
    board = Board.JOBRIGHT

    def __init__(self, selectors: SelectorMap | None = None) -> None:
        super().__init__(
            selectors
            if selectors is not None
            else load_selector_map(Board.JOBRIGHT, required=REQUIRED_SELECTORS)
        )

    async def start_application(self, page: Any) -> ApplyResult:
        autofill_selector = self.selectors.get("autofill_apply_button")
        if autofill_selector and await click_selector(page, autofill_selector):
            return ApplyResult(
                status=ApplyStatus.STARTED,
                reason="clicked the explicit Apply-with-Autofill control",
                clicked="autofill_apply_button",
            )

        clicked = await click_selector(page, self.selectors.require("apply_button"))
        if not clicked:
            return ApplyResult(
                status=ApplyStatus.FAILED,
                reason="no apply control was found on this listing",
            )
        return ApplyResult(
            status=ApplyStatus.STARTED,
            reason="clicked the normal apply control",
            clicked="apply_button",
        )
