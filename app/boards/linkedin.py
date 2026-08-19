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
*confirmed* absent does it look for, and click, the plain external Apply
control: if that check itself cannot be completed (the probe raised, so
presence is genuinely unknown), the safe response is to fail rather than
guess "absent" and click whatever `apply_button` matches on what might
actually be an Easy Apply page underneath.
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
        easy_apply = await probe_selector(page, self.selectors.require("easy_apply_indicator"))
        if easy_apply.outcome is ProbeOutcome.INDETERMINATE:
            return ApplyResult(
                status=ApplyStatus.FAILED,
                reason=(
                    "could not determine whether this listing uses Easy Apply "
                    f"({easy_apply.detail}); refusing to guess and click the "
                    "external apply control instead"
                ),
            )
        if easy_apply.present:
            return ApplyResult(
                status=ApplyStatus.SKIPPED,
                reason=SkipReason.LINKEDIN_EASY_APPLY.value,
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
