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
    # The file doing the ignoring is named in the same sentence and is not
    # itself a claim.
    return [claim for claim in claims if claim != ".gitignore"]


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
            "tests/test_api.py",
            "tests/scripts/test_cli.py",
        ):
            assert path in README.read_text(), f"{path} is no longer documented"
            assert (README.parent / path).exists(), path
