"""Jobright adapter: direct Apply-with-Autofill, plus normal Apply.

Jobright's own listing page can offer an explicit "Apply with Autofill"
control that starts the extension's autofill flow immediately, without
first routing through a plain Apply button. That control is looked for and
clicked *first*, but only when the selector map names one explicitly and it
resolves to exactly one visible element on the page (see
`app.boards.base.attempt_click`); an absent, unconfigured, or ambiguous
match falls through to the normal Apply control rather than guessing which
of several similar-looking buttons is the real one.

That fallback is only safe, though, when nothing was actually dispatched to
the page by the first attempt. If the autofill control was found, confirmed
visible, and `.click()` was invoked but raised, Playwright may already have
sent the click before failing — a mid-click navigation, a detached element,
a timeout after the pointer-down. Clicking the normal Apply control next
would then risk a *second* click on a page already reacting to the first, so
that case is reported as `FAILED` with a diagnostic instead, and the normal
Apply control is never even queried. `ClickAttempt.safe_to_try_another_control`
is exactly this distinction: true for "nothing was dispatched" (absent,
ambiguous, hidden, or the query itself failing), false only once `.click()`
has actually been invoked and raised.

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
    attempt_click,
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
        if autofill_selector:
            attempt = await attempt_click(page, autofill_selector)
            if attempt.clicked:
                return ApplyResult(
                    status=ApplyStatus.STARTED,
                    reason="clicked the explicit Apply-with-Autofill control",
                    clicked="autofill_apply_button",
                )
            if not attempt.safe_to_try_another_control:
                return ApplyResult(
                    status=ApplyStatus.FAILED,
                    reason=(
                        "the explicit Apply-with-Autofill control was clicked, "
                        f"but the click failed or its outcome is unknown ({attempt.detail}); "
                        "refusing to click the normal apply control afterwards, "
                        "since the first click may already have been dispatched"
                    ),
                )
            # Nothing was dispatched (absent, ambiguous, hidden, or the query
            # itself failed): falling back to the normal control is safe.

        fallback = await attempt_click(page, self.selectors.require("apply_button"))
        if not fallback.clicked:
            return ApplyResult(
                status=ApplyStatus.FAILED,
                reason=f"could not click any apply control: {fallback.detail}",
            )
        return ApplyResult(
            status=ApplyStatus.STARTED,
            reason="clicked the normal apply control",
            clicked="apply_button",
        )
