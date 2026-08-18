"""`adapter_for(board)` — the one place board adapters are wired together.

Each call constructs a fresh adapter, which loads and validates that board's
YAML selector map from disk; nothing is cached, so an edited selector file
takes effect on the very next call with no process restart.
"""

from __future__ import annotations

from typing import Callable, Dict

from app.boards.base import BoardAdapter
from app.boards.handshake import HandshakeAdapter
from app.boards.jobright import JobrightAdapter
from app.boards.linkedin import LinkedInAdapter
from app.boards.wellfound import WellfoundAdapter
from app.storage.models import Board

_FACTORIES: Dict[Board, Callable[[], BoardAdapter]] = {
    Board.LINKEDIN: LinkedInAdapter,
    Board.JOBRIGHT: JobrightAdapter,
    Board.WELLFOUND: WellfoundAdapter,
    Board.HANDSHAKE: HandshakeAdapter,
}


def adapter_for(board: Board) -> BoardAdapter:
    """Construct the adapter for `board`, loading and validating its selectors.

    Raises `SelectorMapError` when that board's YAML file is missing,
    malformed, or does not declare a selector its adapter requires — before
    any page is ever touched.
    """
    try:
        factory = _FACTORIES[board]
    except KeyError:
        raise ValueError(f"no board adapter registered for {board!r}") from None
    return factory()
