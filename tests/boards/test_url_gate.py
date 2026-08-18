"""Hardening tests for the listing URL gate (`BaseBoardAdapter.open_listing`).

`urlsplit` (Python's stdlib parser) and a real browser's WHATWG URL parser
disagree about several inputs that matter here: a backslash is a path
separator to a browser but a literal character to `urlsplit`, userinfo can
make either parser disagree about which side of an `@` is the host, and a
handful of URLs that "parse" in Python (malformed ports, IPv6 typos, stray
control characters) are not what a browser would actually navigate to. Every
test below proves the same two things for exactly one shape of confusion:
`open_listing` raises `UntrustedListingUrlError`, and `page.goto` is *never*
called — nothing is requested, let alone rendered, for anything this gate
does not fully trust.
"""

from __future__ import annotations

import pytest

from app.boards.base import UntrustedListingUrlError, host_owned_by_board
from app.boards.linkedin import LinkedInAdapter
from app.storage.models import Board
from tests.boards.support import FakePage

LINKEDIN_SNIPPET = "<button data-test='apply'>Apply</button>"


async def _assert_refused(url: str) -> None:
    adapter = LinkedInAdapter()
    page = FakePage(LINKEDIN_SNIPPET)
    with pytest.raises(UntrustedListingUrlError):
        await adapter.open_listing(page, url)
    assert page.goto_calls == [], f"goto must never be called for a refused URL: {url!r}"


class TestSchemeGate:
    @pytest.mark.parametrize(
        "url",
        [
            "javascript://www.linkedin.com/%0aalert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "ftp://www.linkedin.com/",
            "chrome-extension://abcdefg/popup.html",
            "//www.linkedin.com/jobs/view/1/",
            "www.linkedin.com/jobs/view/1/",
        ],
    )
    async def test_rejects_non_http_https_schemes(self, url: str) -> None:
        await _assert_refused(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://www.linkedin.com/jobs/view/1/",
            "https://www.linkedin.com/jobs/view/1/",
            "HTTPS://WWW.LINKEDIN.COM/jobs/view/1/",
        ],
    )
    async def test_accepts_http_and_https_case_insensitively(self, url: str) -> None:
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_SNIPPET)
        result = await adapter.open_listing(page, url)
        assert result.status.value == "opened"
        assert page.goto_calls == [url]


class TestBackslashConfusion:
    """`https://evil.example\\@linkedin.com/` is the textbook case: Python's
    `urlsplit` reads the backslash as a literal character inside userinfo and
    reports `hostname == "linkedin.com"`, while a real browser treats the
    backslash exactly like `/` for the `https` "special scheme" and navigates
    to `evil.example` with a path of `/@linkedin.com/`. Trusting Python's
    parser here would open an attacker's page while believing it was
    LinkedIn's."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example\\@linkedin.com/",
            "https://evil.example\\@www.linkedin.com/jobs/view/1/",
            "https://linkedin.com\\@evil.example/",
            "https://www.linkedin.com\\.evil.example/",
            "https:\\\\www.linkedin.com\\jobs\\view\\1\\",
            "https://evil.example\\.linkedin.com/jobs/view/1/",
        ],
    )
    async def test_rejects_any_backslash(self, url: str) -> None:
        await _assert_refused(url)

    def test_python_parser_would_have_been_fooled_without_this_guard(self) -> None:
        """Documents *why* the guard above exists: prove the confusion is real."""
        from urllib.parse import urlsplit

        parsed = urlsplit("https://evil.example\\@linkedin.com/")
        assert parsed.hostname == "linkedin.com"

    def test_backslash_dot_suffix_would_pass_naive_suffix_matching_without_this_guard(
        self,
    ) -> None:
        """The sharpest case: with no "@" at all, `urlsplit` reports the whole
        literal string `"evil.example\\.linkedin.com"` as the hostname, which
        *does* end with `".linkedin.com"` — a naive suffix check alone would
        accept it. A real browser, treating the backslash as a path
        separator, navigates to `evil.example` with a path of
        `/.linkedin.com/jobs/view/1/` instead. This is exactly why the
        backslash rejection cannot be replaced by the userinfo check or the
        suffix check alone; each closes a different disagreement."""
        from urllib.parse import urlsplit

        parsed = urlsplit("https://evil.example\\.linkedin.com/jobs/view/1/")
        assert parsed.username is None
        assert (parsed.hostname or "").endswith(".linkedin.com")


class TestUserinfoConfusion:
    @pytest.mark.parametrize(
        "url",
        [
            "https://linkedin.com@evil.example/jobs/view/1/",
            "https://evil.example@linkedin.com/jobs/view/1/",
            "https://www.linkedin.com@evil.example/",
            "https://user:pass@www.linkedin.com/jobs/view/1/",
            "https://:@www.linkedin.com/",
        ],
    )
    async def test_rejects_any_userinfo(self, url: str) -> None:
        await _assert_refused(url)


class TestMalformedPort:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com:99999/jobs/view/1/",
            "https://www.linkedin.com:abc/jobs/view/1/",
            "https://www.linkedin.com:-1/jobs/view/1/",
            "https://www.linkedin.com:65536/",
        ],
    )
    async def test_rejects_malformed_ports(self, url: str) -> None:
        await _assert_refused(url)

    async def test_accepts_a_well_formed_port(self) -> None:
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_SNIPPET)
        result = await adapter.open_listing(page, "https://www.linkedin.com:443/jobs/view/1/")
        assert result.status.value == "opened"


class TestControlCharacters:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.lin\x00kedin.com/jobs/view/1/",
            "https://www.lin\x01kedin.com/jobs/view/1/",
            "https://www.linkedin.com\x7f/jobs/view/1/",
            "https://www.linkedin.com/jobs\x1fview/1/",
        ],
    )
    async def test_rejects_ascii_control_characters(self, url: str) -> None:
        await _assert_refused(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com\t.evil.example/jobs/view/1/",
            "https://www.linkedin.com\n.evil.example/jobs/view/1/",
            "https://www.linkedin.com\r.evil.example/jobs/view/1/",
        ],
    )
    async def test_tab_cr_lf_do_not_smuggle_a_lookalike_host_past_the_gate(
        self, url: str
    ) -> None:
        """`urlsplit` silently deletes tab/CR/LF (matching WHATWG's own first
        parsing step), which could otherwise stitch `linkedin.com` and
        `evil.example` into one host, `linkedin.com.evil.example` — still
        correctly refused by the suffix check, but only because the gate runs
        on the parsed host *after* that deletion, not the pre-image string."""
        await _assert_refused(url)


class TestMalformedHost:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com]/jobs/view/1/",
            "https://[::1/jobs/view/1/",
            "https:///jobs/view/1/",
            "https://[::1]/",
        ],
    )
    async def test_rejects_malformed_or_ip_literal_hosts(self, url: str) -> None:
        await _assert_refused(url)


class TestEncodedLookalikes:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin%2ecom.evil.example/jobs/view/1/",
            "https://www.linkedin.com%00.evil.example/",
            "https://www.linkedin.com%2f@evil.example/",
        ],
    )
    async def test_rejects_percent_encoded_host_lookalikes(self, url: str) -> None:
        await _assert_refused(url)


class TestUnicodeAndIdnaNormalization:
    async def test_rejects_a_cyrillic_homoglyph_domain(self) -> None:
        """A Cyrillic 'і' (U+0456) standing in for Latin 'i' IDNA-encodes to
        `xn--lnkedin-...`, which cannot collide with the plain-ASCII
        `linkedin.com` this gate matches against."""
        await _assert_refused("https://l\u0456nkedin.com/jobs/view/1/")

    async def test_accepts_fullwidth_unicode_that_normalises_to_the_real_domain(
        self,
    ) -> None:
        """Full-width Unicode Latin letters IDNA/nameprep-fold to their plain
        ASCII equivalents — exactly what a real browser's URL parser does —
        so this genuinely is linkedin.com under the hood, not a lookalike."""
        adapter = LinkedInAdapter()
        page = FakePage(LINKEDIN_SNIPPET)
        result = await adapter.open_listing(page, "https://\uff4c\uff49\uff4e\uff4b\uff45\uff44\uff49\uff4e.com/jobs/view/1/")
        assert result.status.value == "opened"

    async def test_rejects_invalid_idna_label(self) -> None:
        await _assert_refused("https://www.linkedin..com/jobs/view/1/")


class TestNeverGotoOnRejection:
    async def test_goto_is_never_called_across_every_rejected_shape(self) -> None:
        urls = [
            "javascript://www.linkedin.com/",
            "https://evil.example\\@linkedin.com/",
            "https://linkedin.com@evil.example/",
            "https://www.linkedin.com:abc/",
            "https://www.lin\x00kedin.com/",
            "https://l\u0456nkedin.com/",
            "not-a-url",
            "",
        ]
        for url in urls:
            adapter = LinkedInAdapter()
            page = FakePage(LINKEDIN_SNIPPET)
            with pytest.raises(UntrustedListingUrlError):
                await adapter.open_listing(page, url)
            assert page.goto_calls == []


class TestHostOwnedByBoardHelper:
    """`host_owned_by_board` mirrors the same hardening as a plain boolean
    query, for callers that want a predicate rather than an exception."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example\\@linkedin.com/",
            "https://linkedin.com@evil.example/",
            "https://linkedin.com:abc/",
            "javascript://linkedin.com/",
        ],
    )
    def test_returns_false_for_every_hardened_rejection(self, url: str) -> None:
        assert host_owned_by_board(url, Board.LINKEDIN) is False

    def test_returns_true_for_a_genuine_url(self) -> None:
        assert host_owned_by_board("https://www.linkedin.com/jobs/view/1/", Board.LINKEDIN) is True
