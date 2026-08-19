"""Handshake adapter: a normal, single Apply control.

Handshake has no unsupported in-page modal comparable to LinkedIn's Easy
Apply or Wellfound's in-app flow, so this is the simplest of the four
adapters: find and click the one configured Apply control.
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

REQUIRED_SELECTORS: tuple[str, ...] = ("apply_button",)


class HandshakeAdapter(BaseBoardAdapter):
    board = Board.HANDSHAKE

    def __init__(self, selectors: SelectorMap | None = None) -> None:
        super().__init__(
            selectors
            if selectors is not None
            else load_selector_map(Board.HANDSHAKE, required=REQUIRED_SELECTORS)
        )

    async def start_application(self, page: Any) -> ApplyResult:
        attempt = await attempt_click(page, self.selectors.require("apply_button"))
        if not attempt.clicked:
            return ApplyResult(
                status=ApplyStatus.FAILED,
                reason=f"could not click the apply control: {attempt.detail}",
            )
        return ApplyResult(
            status=ApplyStatus.STARTED,
            reason="clicked the apply control",
            clicked="apply_button",
        )
