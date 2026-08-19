"""Browser fixtures for the integration suite, and the skips that guard them.

Kept in a conftest rather than in the test module so that a second browser
test file cannot end up launching Chrome a second, differently-configured
way — the launch flags are the part of this suite that is easiest to get
subtly wrong and hardest to notice.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
import pytest_asyncio

from tests.integration.browser import (
    IGNORED_DEFAULT_ARGS,
    VirtualDisplay,
    find_browser,
    launch_arguments,
    loopback_only,
)

#: How long the extension's content script is given to attach its sidebar.
#: Chrome does not inject a freshly-loaded unpacked extension's content
#: scripts into tabs that already exist, so the page is reloaded between
#: attempts rather than merely waited on again.
SIDEBAR_TIMEOUT_MS = 5_000
SIDEBAR_ATTEMPTS = 4


@pytest.fixture(scope="session")
def display() -> Iterator[str]:
    screen = VirtualDisplay()
    screen.start()
    if screen.skip_reason:
        pytest.skip(screen.skip_reason)
    try:
        yield screen.display
    finally:
        screen.stop()


@pytest.fixture(scope="session")
def chrome() -> str:
    prerequisites = find_browser()
    if not prerequisites.available:
        pytest.skip(prerequisites.skip_reason)
    return prerequisites.executable


@pytest_asyncio.fixture(loop_scope="function")
async def context(display: str, chrome: str, tmp_path: Path) -> AsyncIterator[Any]:
    """A headed persistent context with the stub extension loaded.

    A throwaway `user_data_dir` under `tmp_path`, never the operator's
    profile: these tests fill in forms and click submit buttons, and doing
    that in a profile somebody is signed into is the one thing this project
    promises not to do.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch_persistent_context(
            str(tmp_path / "profile"),
            headless=False,
            executable_path=chrome,
            ignore_default_args=list(IGNORED_DEFAULT_ARGS),
            args=launch_arguments(),
            # The whole environment, not just DISPLAY: replacing it would drop
            # XAUTHORITY and leave the browser unable to authenticate to an X
            # server it can otherwise see.
            env={**os.environ, "DISPLAY": display},
            viewport={"width": 1280, "height": 1024},
        )
        try:
            yield browser
        finally:
            await browser.close()


@pytest_asyncio.fixture(loop_scope="function")
async def page(context: Any, fixture_server: str) -> AsyncIterator[Any]:
    """The Greenhouse fixture, open, with the stub's sidebar attached."""
    opened = context.pages[0] if context.pages else await context.new_page()
    url = loopback_only(f"{fixture_server}/ats/greenhouse.html")
    await opened.goto(url, wait_until="load")
    await _await_sidebar(opened)
    yield opened


async def _await_sidebar(page: Any) -> None:
    """Wait for the content script to attach its open shadow root.

    A reload between attempts, because the failure this works around is not
    slowness: Chrome will not inject a just-registered extension's content
    scripts into a tab that already existed, and no amount of waiting on
    that tab changes it.
    """
    for _ in range(SIDEBAR_ATTEMPTS):
        try:
            await page.wait_for_function(
                "() => Boolean(document.getElementById('jobright-stub-host')"
                "?.shadowRoot)",
                timeout=SIDEBAR_TIMEOUT_MS,
            )
            return
        except Exception:  # noqa: BLE001 - retried, then turned into a skip
            await page.reload(wait_until="load")

    pytest.skip(
        "the stub extension's content script never attached its sidebar after "
        f"{SIDEBAR_ATTEMPTS} page loads, so this Chromium is not running "
        "unpacked MV3 extensions: check that the browser is Chrome or Chromium "
        "(not headless shell) and that tests/fixtures/fake_extension is intact"
    )
