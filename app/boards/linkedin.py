"""LinkedIn adapter: normal external Apply is supported; Easy Apply is not.

LinkedIn's "Easy Apply" opens LinkedIn's own multi-step modal, entirely
in-page on linkedin.com — there is no external ATS page for the rest of the
graph to detect or drive, and guessing through an unfamiliar modal's
questions is exactly the kind of blind interaction this project exists to
avoid. A plain "Apply" button, by contrast, navigates to the employer's own
application page (frequently a supported ATS), which the rest of the graph
already knows how to handle.

`start_application` therefore checks for the Easy Apply indicator *first*
and skips the whole listing the moment it is present, before any other
selector on the page is even queried — so the modal is never entered, and
`apply_button` is never clicked underneath it. Only when Easy Apply is
absent does it look for, and click, the plain external Apply control.
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

REQUIRED_SELECTORS: tuple[str, ...] = ("easy_apply_indicator", "apply_button")


class LinkedInAdapter(BaseBoardAdapter):
    board = Board.LINKEDIN

    def __init__(self, selectors: SelectorMap | None = None) -> None:
        super().__init__(
            selectors
            if selectors is not None
            else load_selector_map(Board.LINKEDIN, required=REQUIRED_SELECTORS)
        )

    async def start_application(self, page: Any) -> ApplyResult:
        if await selector_present(page, self.selectors.require("easy_apply_indicator")):
            return ApplyResult(
                status=ApplyStatus.SKIPPED,
                reason=SkipReason.LINKEDIN_EASY_APPLY.value,
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
