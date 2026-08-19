"""The README is checked, not just written.

Documentation that does not run is documentation that quietly stops being
true. Two things here are cheap to verify and expensive to get wrong: the
answers-file example (an operator copies it, and a YAML file that does not
parse is rejected whole, so a broken example costs them a debugging
session), and the commands the README tells people to type (a subcommand
that was renamed leaves the prose pointing at nothing).

This is a documentation test, not a prose test. It has nothing to say about
whether the README is *good*; it only refuses to let it be wrong about the
code in the same repository.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from app.agent.gap_filler import AnswerBook

README = Path(__file__).resolve().parents[1] / "README.md"


def _blocks(language: str) -> list[str]:
    return re.findall(
        rf"^```{language}\n(.*?)^```", README.read_text(), re.MULTILINE | re.DOTALL
    )


REPO = README.parent


def _claimed_gitignored() -> list[str]:
    """Every path the README says git will not take.

    Read out of the prose rather than listed here, so a new claim is
    checked by the act of making it.
    """
    claims: list[str] = []
    for line in README.read_text().splitlines():
        if "gitignored" in line or "is in `.gitignore`" in line:
            claims.extend(re.findall(r"`([^`]+)`", line))
    # A sentence about ignoring can mention things that are not paths — a
    # setting name, or the ignore file itself.
    return [
        claim
        for claim in claims
        if claim != ".gitignore" and ("/" in claim or "." in claim)
    ]


def _git_ignores(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "--", path],
        cwd=REPO,
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        pytest.skip(f"git could not answer: {result.stderr.decode().strip()}")
    return result.returncode == 0


class TestThePrivacyClaims:
    """The README promises git will not take these. Git is asked, not trusted.

    Every one of them is somebody's real data: the log of jobs they applied
    to, screenshots of half-filled application forms, the answers file with
    their address and salary history, and the `.env` holding an API key. A
    claim like this one is only worth making if a `git add -A` in a hurry
    cannot quietly break it.
    """

    def test_the_readme_makes_a_claim_to_check(self) -> None:
        """Guards the tests below: an empty claim list proves nothing."""
        assert _claimed_gitignored()

    @pytest.mark.parametrize("claim", _claimed_gitignored())
    def test_every_claimed_path_is_ignored_by_git(self, claim: str) -> None:
        # A directory claim is checked through a file inside it: `data/` is
        # only a real promise if it covers the screenshots underneath.
        path = claim + "screenshot.png" if claim.endswith("/") else claim
        assert _git_ignores(path), f"the README says {claim} is gitignored, and it is not"

    @pytest.mark.parametrize(
        "path",
        [
            "data/jobs.db",
            "data/artifacts/apply-1.png",
            "data/artifacts/nested/apply-2.png",
            "data/checkpoints.sqlite",
            "answers.yaml",
            ".env",
        ],
    )
    def test_the_files_a_run_actually_produces_are_ignored(self, path: str) -> None:
        """The defaults in `.env.example`, spelled out.

        `*.db` covered the database and nothing else: a screenshot of a
        filled-in application form under `data/artifacts/` was staged by
        any `git add -A`.
        """
        assert _git_ignores(path)

    def test_none_of_it_is_in_the_repository_already(self) -> None:
        """An ignore rule does nothing for a file that is already tracked."""
        tracked = subprocess.run(
            ["git", "ls-files", "--", "data", "answers.yaml", ".env"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        assert tracked.stdout.strip() == ""

    def test_the_example_files_are_still_committable(self) -> None:
        """Ignoring too much is its own failure: these must be shareable."""
        assert not _git_ignores("answers.example.yaml")
        assert not _git_ignores(".env.example")


class TestTheAnswersExample:
    def test_the_readme_ships_a_yaml_example(self) -> None:
        """Guards the test below: a regex that matches nothing proves nothing."""
        assert _blocks("yaml")

    @pytest.mark.parametrize("index", range(len(_blocks("yaml"))))
    def test_every_yaml_example_loads_as_an_answer_book(self, index: int) -> None:
        block = _blocks("yaml")[index]
        book = AnswerBook.from_mapping(yaml.safe_load(block), source="README.md")
        assert len(book) > 0


class TestTheWarnings:
    """Three things an operator is worse off not knowing.

    Each was true of the code and absent from the prose, which is the worst
    combination: the reader forms a belief the system does not share. A
    keyword check is a crude test, but it fails if someone deletes the
    paragraph, which is what it is for.
    """

    def test_the_build_says_that_approving_submits(self) -> None:
        """The old warning said the opposite, and the code changed under it.

        An operator who read "approving does not submit" and still believes
        it is the worst possible reader of this build: they would approve
        things to see what staging looks like.
        """
        text = README.read_text()
        assert "ComponentNotWired" not in text
        assert "does not submit it" not in text
        assert "Approving submits the application" in text

    def test_the_warning_sits_where_approval_is_explained(self) -> None:
        """Buried at the bottom it would be read after the surprise."""
        text = README.read_text()
        approving = text.index("## Approving and rejecting")
        exporting = text.index("## Exporting the log")
        assert approving < text.index("Approving submits the application") < exporting

    def test_the_submit_limitations_are_documented(self) -> None:
        """What will and will not be clicked, in the operator's own terms.

        Each of these is a case where the agent stops rather than guesses,
        and an operator who does not know about it reads the resulting
        `failed` row as a malfunction.
        """
        text = README.read_text()
        assert "Submit application" in text
        assert "`Apply`" in text
        assert "Next" in text and "Continue" in text
        assert "unconfirmed" in text

    def test_what_counts_as_a_confirmed_submission_is_documented(self) -> None:
        """And, just as importantly, what does not.

        The prose used to promise that a navigation confirmed a submission,
        which is how a sign-in redirect became a submitted application. An
        operator reading the old sentence would trust exactly the rows they
        should not.
        """
        text = README.read_text()
        submitting = text.index("Approving submits the application")
        section = text[submitting : text.index("## The HTTP API")]
        assert "navigat" in section
        assert "confirmation" in section
        assert "disappear" in section or "no longer" in section
        assert "navigation on its own is not" in section
        assert "not already showing" in section
        # The freshness rule an operator can act on: a panel that keeps
        # changing its own wording is not a stream of confirmations.
        assert "however its wording changes" in section
        # And the rule that makes the wording secondary in the first place.
        assert "Nothing is confirmed on words alone" in section
        assert "no longer\n  visible" in section or "no longer visible" in section

    def test_a_refusal_before_the_press_is_documented_as_costing_nothing(
        self,
    ) -> None:
        """Otherwise the honest advice after one is "give up on this row".

        A refused press leaves the application submittable, and an operator
        who does not know that will not fix the page and try again.
        """
        text = README.read_text()
        assert "immediately before it is made" in text
        assert "as submittable as it was" in text

    def test_the_readme_says_the_click_never_happens_twice(self) -> None:
        """The one thing worse than an unconfirmed submission is two."""
        text = README.read_text()
        assert "never clicked a second time" in text

    def test_a_worker_killed_mid_submit_is_documented(self) -> None:
        """The only crash in this system whose damage cannot be undone.

        Every other interruption is recovered by re-running the node. This
        one cannot be, and an operator who does not know that will read the
        failure as "it did not happen" and retry it by hand.
        """
        text = README.read_text()
        assert "killed" in text
        killed = text.index("killed mid-submit")
        window = text[killed - 800 : killed + 800]
        assert "not pressed again" in window or "is not clicked again" in window

    def test_queueing_is_documented_as_not_idempotent(self) -> None:
        text = README.read_text()
        assert "not idempotent" in text
        assert text.count("per queue item, not per") >= 1

    def test_every_internal_link_lands_on_a_heading(self) -> None:
        """A `#link` to a renamed section fails silently and forever."""
        text = README.read_text()
        headings = {
            re.sub(r"[^a-z0-9 -]", "", line.lstrip("#").strip().lower()).replace(
                " ", "-"
            )
            for line in text.splitlines()
            if line.startswith("#")
        }
        targets = set(re.findall(r"\]\(#([\w-]+)\)", text))
        assert targets, "the README stopped cross-referencing itself"
        assert targets <= headings, targets - headings

    def test_the_token_exposure_risks_are_documented(self) -> None:
        text = README.read_text()
        assert "no TLS" in text or "plain HTTP" in text
        assert "ps auxww" in text


class TestTheCommands:
    """Every command the README tells an operator to run still exists."""

    def test_the_documented_subcommands_are_real(self) -> None:
        from scripts.export_log import build_parser as export_parser
        from scripts.run_batch import build_parser as batch_parser

        text = README.read_text()
        for command in ("queue", "status", "pending", "run", "approve", "reject"):
            assert f"scripts.run_batch {command}" in text or (
                f"scripts.run_batch --local {command}" in text
                or f"run_batch --json {command}" in text
            ), command

        batch = batch_parser()
        documented = {"queue", "status", "pending", "run", "approve", "reject"}
        subparsers = next(
            action for action in batch._actions if hasattr(action, "choices")
            and isinstance(getattr(action, "choices", None), dict)
        )
        assert documented == set(subparsers.choices)

        # Only that it parses: the exporter's flags are behaviour-tested in
        # tests/scripts/test_cli.py, and duplicating that here would pin the
        # same thing twice.
        export_parser().parse_args(["--format", "csv", "--fields", "--include-values"])

    def test_no_command_names_a_module_that_is_not_there(self) -> None:
        """First-party only: `python -m mypy` is not ours to find on disk."""
        modules = {
            module
            for module in re.findall(r"python -m ([\w.]+)", README.read_text())
            if module.split(".")[0] in {"app", "scripts", "tests"}
        }
        assert modules, "the README stopped documenting any command at all"
        for module in modules:
            path = Path(*module.split(".")).with_suffix(".py")
            assert (README.parent / path).exists(), module

    def test_every_documented_tool_is_a_declared_dev_dependency(self) -> None:
        """`pip install -e '.[dev]'` must be enough to run what is documented.

        The README's own checks section says to run mypy, and `types-PyYAML`
        was in the dev extras — a stub package for a type checker nobody was
        told to install. Someone following the README got
        `No module named mypy` and no clue whether that was their mistake.
        """
        extras = tomllib.loads((REPO / "pyproject.toml").read_text())["project"][
            "optional-dependencies"
        ]["dev"]
        declared = {re.split(r"[<>=!\[ ]", spec)[0].lower() for spec in extras}

        third_party = {
            module
            for module in re.findall(r"python -m ([\w.]+)", README.read_text())
            if module.split(".")[0] not in {"app", "scripts", "tests"}
        }
        assert third_party, "the README stopped documenting any tool at all"
        for module in third_party:
            assert module.split(".")[0].lower() in declared, module

    def test_no_documented_path_is_missing(self) -> None:
        """Paths the README points at — fixtures, selectors, examples."""
        for path in (
            "tests/fixtures/ats",
            "tests/fixtures/fake_extension",
            "tests/fixtures/stub_gap_contract.json",
            "app/boards/selectors",
            "answers.example.yaml",
            ".env.example",
            "tests/test_fixture_server.py",
            "tests/test_stub_extension_contract.py",
            "tests/integration/test_stub_extension.py",
            "tests/test_api.py",
            "tests/scripts/test_cli.py",
        ):
            assert path in README.read_text(), f"{path} is no longer documented"
            assert (README.parent / path).exists(), path
