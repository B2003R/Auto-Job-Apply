"""Tests for ATS classification from a listing URL and its DOM.

Classification is pure: no browser, no network, no fixtures beyond the static
HTML recorded in Task 2. The interesting cases are the *negative* ones — a URL
that merely mentions an ATS host, prose about greenhouse gases, a lookalike
domain — because a wrong classification sends the graph down an adapter path
built for a different form, while `UNKNOWN` is a typed, recoverable skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.ats_detector import (
    AtsDetection,
    AtsKind,
    AtsSignal,
    classify_ats,
    detect_ats,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ats"


def fixture_html(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.html").read_text(encoding="utf-8")


class TestKnownHosts:
    @pytest.mark.parametrize(
        "url,expected",
        [
            (
                "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Engineer",
                AtsKind.WORKDAY,
            ),
            ("https://acme.wd5.myworkdaysite.com/recruiting/acme/careers", AtsKind.WORKDAY),
            ("https://www.myworkday.com/acme/d/inst/job", AtsKind.WORKDAY),
            ("https://boards.greenhouse.io/acme/jobs/4123456", AtsKind.GREENHOUSE),
            ("https://job-boards.greenhouse.io/acme/jobs/4123456", AtsKind.GREENHOUSE),
            ("https://grnh.se/abc123", AtsKind.GREENHOUSE),
            ("https://jobs.lever.co/acme/6f2a-4a2e", AtsKind.LEVER),
            ("https://jobs.eu.lever.co/acme/6f2a-4a2e", AtsKind.LEVER),
            ("https://hire.lever.co/applications/6f2a", AtsKind.LEVER),
            ("https://careers-acme.icims.com/jobs/1234/engineer/job", AtsKind.ICIMS),
            ("https://jobs.smartrecruiters.com/Acme/743999", AtsKind.SMARTRECRUITERS),
            ("https://careers.smartrecruiters.com/Acme/743999", AtsKind.SMARTRECRUITERS),
        ],
    )
    def test_recognises_each_supported_ats_from_its_host(
        self, url: str, expected: AtsKind
    ) -> None:
        assert detect_ats(url, "") is expected

    def test_host_matching_ignores_case_port_and_credentials(self) -> None:
        assert detect_ats("HTTPS://BOARDS.GREENHOUSE.IO:443/acme/jobs/1", "") is (
            AtsKind.GREENHOUSE
        )
        assert detect_ats("https://user:pw@jobs.lever.co/acme/1", "") is AtsKind.LEVER

    def test_bare_registrable_domain_counts(self) -> None:
        assert detect_ats("https://greenhouse.io/careers", "") is AtsKind.GREENHOUSE
        assert detect_ats("https://lever.co/", "") is AtsKind.LEVER


class TestUnknownFallback:
    @pytest.mark.parametrize(
        "url",
        [
            "https://careers.acme.com/apply/1234",
            "https://www.example.org/jobs",
            "",
            "not-a-url",
            "about:blank",
            "file:///tmp/apply.html",
        ],
    )
    def test_unrecognised_pages_are_unknown(self, url: str) -> None:
        assert detect_ats(url, "<html><body><form></form></body></html>") is AtsKind.UNKNOWN

    def test_empty_html_is_tolerated(self) -> None:
        assert detect_ats("https://careers.acme.com/apply", "") is AtsKind.UNKNOWN

    def test_html_may_be_omitted_entirely(self) -> None:
        assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") is AtsKind.GREENHOUSE

    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io.evil.example/acme/jobs/1",
            "https://greenhouse.io.attacker.test/apply",
            "https://notlever.co/acme/1",
            "https://mylever.co/acme/1",
            "https://icims.com.phishing.test/jobs/1",
            "https://fakemyworkdayjobs.com/en-US/careers",
        ],
    )
    def test_lookalike_hosts_are_not_matched(self, url: str) -> None:
        assert detect_ats(url, "") is AtsKind.UNKNOWN

    def test_ats_host_mentioned_only_in_the_query_string_is_not_matched(self) -> None:
        url = "https://careers.acme.com/apply?utm_source=boards.greenhouse.io&next=jobs.lever.co"
        assert detect_ats(url, "") is AtsKind.UNKNOWN

    def test_prose_mentioning_an_ats_word_is_not_a_marker(self) -> None:
        html = """
        <html><body>
          <h1>Sustainability Engineer</h1>
          <p>Help us cut greenhouse gas emissions and leverage clean energy.</p>
          <p>We use levers of change; our icims of success are clear.</p>
        </body></html>
        """
        assert detect_ats("https://careers.acme.com/apply", html) is AtsKind.UNKNOWN

    @pytest.mark.parametrize(
        "markup",
        [
            '<img alt="Our greenhouse in winter">',
            '<input placeholder="Describe how you would lever open new markets">',
            '<textarea title="Why icims matter to you"></textarea>',
            '<input type="submit" value="Apply via Greenhouse">',
            '<abbr aria-label="Workday means something else here">shift</abbr>',
        ],
    )
    def test_prose_inside_user_facing_attributes_is_not_a_marker(self, markup: str) -> None:
        html = f"<html><body><form>{markup}</form></body></html>"
        assert detect_ats("https://careers.acme.com/apply", html) is AtsKind.UNKNOWN

    def test_markers_inside_html_comments_are_ignored(self) -> None:
        html = '<html><body><!-- <iframe src="https://boards.greenhouse.io/embed/job_app"> --></body></html>'
        assert detect_ats("https://careers.acme.com/apply", html) is AtsKind.UNKNOWN


class TestDomMarkers:
    def test_embedded_greenhouse_iframe_on_a_company_domain(self) -> None:
        html = """
        <html><body>
          <div id="grnhse_app"></div>
          <iframe src="https://boards.greenhouse.io/embed/job_app?token=123"></iframe>
        </body></html>
        """
        assert detect_ats("https://careers.acme.com/jobs/1", html) is AtsKind.GREENHOUSE

    def test_workday_automation_attributes(self) -> None:
        html = """
        <html><body>
          <div data-automation-id="jobPostingHeader">Engineer</div>
          <button data-automation-id="applyButton">Apply</button>
        </body></html>
        """
        assert detect_ats("https://careers.acme.com/jobs/1", html) is AtsKind.WORKDAY

    def test_icims_iframe_marker(self) -> None:
        html = '<html><body><iframe id="icimsJobsIframe" src="https://careers-acme.icims.com/jobs/1"></iframe></body></html>'
        assert detect_ats("https://careers.acme.com/jobs/1", html) is AtsKind.ICIMS

    def test_smartrecruiters_script_marker(self) -> None:
        html = '<html><head><script src="https://www.smartrecruiters.com/widget.js"></script></head></html>'
        assert detect_ats("https://careers.acme.com/jobs/1", html) is (
            AtsKind.SMARTRECRUITERS
        )

    def test_lever_class_marker_without_a_lever_url(self) -> None:
        html = '<html><body><form class="lever-application-form"></form></body></html>'
        assert detect_ats("https://careers.acme.com/jobs/1", html) is AtsKind.LEVER

    def test_meta_generator_marker(self) -> None:
        html = '<html><head><meta name="generator" content="Greenhouse"></head></html>'
        assert detect_ats("https://careers.acme.com/jobs/1", html) is AtsKind.GREENHOUSE

    def test_the_url_wins_when_the_dom_disagrees(self) -> None:
        html = '<html><body><form class="lever-application-form"></form></body></html>'
        url = "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Engineer"
        assert detect_ats(url, html) is AtsKind.WORKDAY

    def test_equally_supported_conflicting_dom_markers_stay_unknown(self) -> None:
        html = """
        <html><body>
          <form class="lever-application-form"></form>
          <form class="greenhouse-application-form"></form>
        </body></html>
        """
        detection = classify_ats("https://careers.acme.com/jobs/1", html)
        assert detection.kind is AtsKind.UNKNOWN
        assert detection.ambiguous is True

    def test_malformed_markup_does_not_raise(self) -> None:
        html = "<html><body><div class=unquoted-lever <iframe src= ></body>"
        assert isinstance(detect_ats("https://careers.acme.com", html), AtsKind)


class TestRecordedFixtures:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("greenhouse", AtsKind.GREENHOUSE),
            ("lever", AtsKind.LEVER),
            ("workday", AtsKind.WORKDAY),
            ("unknown", AtsKind.UNKNOWN),
        ],
    )
    def test_offline_fixtures_classify_as_recorded(
        self, name: str, expected: AtsKind
    ) -> None:
        assert detect_ats("http://127.0.0.1:9/ats/" + name, fixture_html(name)) is expected


class TestDetectionEvidence:
    def test_detection_reports_the_signals_it_used(self) -> None:
        detection = classify_ats("https://boards.greenhouse.io/acme/jobs/1", "")

        assert isinstance(detection, AtsDetection)
        assert detection.kind is AtsKind.GREENHOUSE
        assert detection.signals
        assert all(isinstance(signal, AtsSignal) for signal in detection.signals)
        assert any(signal.source == "url_host" for signal in detection.signals)
        assert detection.confidence > 0.0
        assert detection.ambiguous is False

    def test_unknown_detection_carries_no_signals_and_zero_confidence(self) -> None:
        detection = classify_ats("https://careers.acme.com/apply", "<html></html>")

        assert detection.kind is AtsKind.UNKNOWN
        assert detection.signals == ()
        assert detection.confidence == 0.0

    def test_signals_never_carry_raw_page_markup(self) -> None:
        html = '<html><body><input name="secret_token" value="super-secret-value"></body></html>'
        detection = classify_ats("https://boards.greenhouse.io/acme/jobs/1", html)

        rendered = repr(detection)
        assert "super-secret-value" not in rendered
        assert "secret_token" not in rendered

    def test_detection_is_immutable(self) -> None:
        detection = classify_ats("https://jobs.lever.co/acme/1", "")

        with pytest.raises(Exception):
            detection.kind = AtsKind.UNKNOWN  # type: ignore[misc]
        assert isinstance(detection.signals, tuple)

    def test_classification_is_deterministic(self) -> None:
        html = fixture_html("workday")
        first = classify_ats("https://careers.acme.com/apply", html)
        second = classify_ats("https://careers.acme.com/apply", html)

        assert first == second


class TestPersistedValues:
    def test_kind_values_are_the_persisted_strings(self) -> None:
        assert AtsKind.WORKDAY.value == "workday"
        assert AtsKind.GREENHOUSE.value == "greenhouse"
        assert AtsKind.LEVER.value == "lever"
        assert AtsKind.ICIMS.value == "icims"
        assert AtsKind.SMARTRECRUITERS.value == "smartrecruiters"
        assert AtsKind.UNKNOWN.value == "unknown"

    def test_unknown_is_the_only_unsupported_marker(self) -> None:
        supported = {kind for kind in AtsKind if kind is not AtsKind.UNKNOWN}
        assert AtsKind.UNKNOWN not in supported
        assert len(supported) == 5
