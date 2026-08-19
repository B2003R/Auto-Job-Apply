"""The offline ATS fixtures answer a submit the way a real ATS would.

A fixture whose submit button navigated to nothing would let a browser test
"pass" on a blank page, which is the failure this file exists to prevent:
the headed integration test only reports a submission when the page produces
one of the three signals `PlaywrightSubmitter` accepts, so the fixtures have
to produce one locally, with no network and no server-side handler.

The script is shared by every fixture and does three things on submit:
cancels the navigation, replaces the form with a visible confirmation, and
removes the form from the document. Two of the three accepted signals, from
one handler, on loopback.

These are text checks on files, not browser tests — but the confirmation
wording is checked against the *same* predicate the submitter uses, so a
fixture cannot drift into confirming something the shipped code would not
recognise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agent.browser_actions import is_confirmation_text
from tests.fixture_server import ATS_FIXTURES, NESTED_FIXTURES

ATS_DIR = Path(__file__).resolve().parent / "fixtures" / "ats"
FAKE_SUBMIT_JS = ATS_DIR / "fake_submit.js"
SHADOW_FORM_JS = ATS_DIR / "shadow_form.js"


def _html(slug: str) -> str:
    return (ATS_DIR / f"{slug}.html").read_text(encoding="utf-8")


def _confirmation_wording(script: Path = FAKE_SUBMIT_JS) -> str:
    """The text the fixtures show, read out of the script itself."""
    match = re.search(r"CONFIRMATION_TEXT\s*=\s*\"([^\"]+)\"", script.read_text())
    assert match is not None, f"{script.name} must declare CONFIRMATION_TEXT"
    return match.group(1)


def test_the_fixtures_share_one_submit_handler() -> None:
    """Four copies of this would drift; the drift would be a false pass."""
    assert FAKE_SUBMIT_JS.exists()


@pytest.mark.parametrize("slug", ATS_FIXTURES)
def test_every_fixture_loads_the_handler(slug: str) -> None:
    assert "fake_submit.js" in _html(slug)


def test_the_handler_cancels_the_navigation() -> None:
    """A real ATS posts; a fixture that posted would leave the page."""
    assert "preventDefault" in FAKE_SUBMIT_JS.read_text()


def test_the_handler_removes_the_form_it_submitted() -> None:
    source = FAKE_SUBMIT_JS.read_text()
    assert "remove()" in source


def test_the_handler_shows_a_confirmation_the_submitter_recognises() -> None:
    """The one assertion here that is not a text search.

    The fixture's wording is run through the shipped predicate, so a
    reworded confirmation that the submitter would ignore fails here rather
    than in a browser test nobody can run on this machine.
    """
    assert is_confirmation_text(_confirmation_wording())


def test_the_confirmation_is_in_a_region_the_submitter_looks_at() -> None:
    source = FAKE_SUBMIT_JS.read_text()
    assert 'role", "status"' in source or "role=\"status\"" in source


def test_nothing_is_submitted_anywhere() -> None:
    """No action, no method, no fetch: the handler is the whole story."""
    source = FAKE_SUBMIT_JS.read_text()
    assert "fetch(" not in source
    assert "XMLHttpRequest" not in source
    for slug in ATS_FIXTURES:
        assert "action=" not in _html(slug)


@pytest.mark.parametrize("slug", ATS_FIXTURES)
def test_the_form_still_validates_before_it_confirms(slug: str) -> None:
    """`novalidate` would let a half-filled form confirm.

    The point of the integration test is that the writer filled the last
    required input; a fixture that submitted regardless would prove nothing
    about the writer.
    """
    assert "novalidate" not in _html(slug)


# --------------------------------------------------------------------------
# The fixtures whose form is not in the top document
# --------------------------------------------------------------------------


@pytest.mark.parametrize("slug", NESTED_FIXTURES)
def test_a_nested_fixture_submits_nowhere_either(slug: str) -> None:
    """The same promise as above, for the iframe and shadow-root pages."""
    assert "action=" not in _html(slug)


def test_the_shadow_fixture_answers_its_own_press() -> None:
    """A control in a shadow root is not the light-DOM form's control.

    Neither native submission nor native constraint validation reaches
    across the boundary, so the fixture has to do both itself — and a
    fixture that only did the first would confirm a form whose gap was
    never filled, which would prove nothing about the writer.
    """
    source = SHADOW_FORM_JS.read_text()

    assert "preventDefault" in source
    assert "is required" in source
    assert "fetch(" not in source
    assert "XMLHttpRequest" not in source


def test_the_two_handlers_confirm_in_the_same_words() -> None:
    """Two scripts is one more than one; drift would be a false negative.

    The shadow fixture cannot share `fake_submit.js` — it has its own
    reasons to intercept — so the wording is asserted equal instead of
    asserted twice.
    """
    assert _confirmation_wording(SHADOW_FORM_JS) == _confirmation_wording()
    assert is_confirmation_text(_confirmation_wording(SHADOW_FORM_JS))


def test_the_iframe_host_baits_the_bug_it_exists_for() -> None:
    """The standing banner has to read like a confirmation to be bait.

    The whole point of `iframe_host.html` is that a submitter judging a
    child frame's press by the top page would read this text as this
    application being confirmed. Worded so the shipped predicate ignores
    it, the fixture would pass whether or not the bug was fixed.
    """
    host = _html("iframe_host")
    match = re.search(r'role="status">\s*([^<]+)', host)
    assert match is not None

    assert is_confirmation_text(match.group(1).strip())
    assert "<form" not in host
