"""Tests for the offline ATS fixture server."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from tests.fixture_server import ATS_FIXTURES


ATS_FORM_MARKERS: dict[str, list[str]] = {
    "greenhouse": [
        'id="application-form"',
        'data-source="greenhouse"',
        'name="first_name"',
        'name="last_name"',
        'name="email"',
        'name="phone"',
        'name="resume"',
        'name="cover_letter"',
    ],
    "lever": [
        'class="lever-application-form"',
        'data-qa="application-form"',
        'name="name"',
        'name="email"',
        'name="phone"',
        'name="comments"',
    ],
    "workday": [
        'data-automation-id="jobApplicationPage"',
        'data-automation-id="legalNameSection_firstName"',
        'data-automation-id="legalNameSection_lastName"',
        'data-automation-id="email"',
        'data-automation-id="phone"',
        'data-automation-id="additionalInformation"',
    ],
    "unknown": [
        'class="generic-application-form"',
        'name="applicant_name"',
        'name="applicant_email"',
        'name="applicant_phone"',
        'name="additional_notes"',
    ],
}


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8")


def test_fixture_server_binds_loopback_only(fixture_server: str) -> None:
    assert fixture_server.startswith("http://127.0.0.1:")


@pytest.mark.parametrize("slug", ATS_FIXTURES)
def test_ats_fixture_form_markers(fixture_server: str, slug: str) -> None:
    url = f"{fixture_server}/ats/{slug}.html"
    html = _fetch(url)

    for marker in ATS_FORM_MARKERS[slug]:
        assert marker in html, f"{slug} missing marker {marker!r}"


def test_fixture_server_missing_page_returns_404(fixture_server: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _fetch(f"{fixture_server}/ats/does-not-exist.html")
    assert exc_info.value.code == 404
