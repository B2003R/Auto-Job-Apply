"""Contract tests for the MV3 stub extension gap invariants."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.fixture_server import ATS_FIXTURES

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CONTRACT_PATH = FIXTURES_DIR / "stub_gap_contract.json"
EXTENSION_DIR = FIXTURES_DIR / "fake_extension"
JS_CONTRACT_PATH = EXTENSION_DIR / "stub_gap_contract.js"
CONTENT_JS_PATH = EXTENSION_DIR / "content.js"
MANIFEST_PATH = EXTENSION_DIR / "manifest.json"

INTENTIONAL_REQUIRED_GAPS = {
    "greenhouse": "last_name",
    "lever": "name",
    "workday": "firstName",
    "unknown": "applicant_phone",
}

INTENTIONAL_TEXTAREA_GAPS = {
    "greenhouse": "cover_letter",
    "lever": "comments",
    "workday": "additionalInformation",
    "unknown": "additional_notes",
}


class _FormFieldParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"input", "textarea", "select"}:
            return
        attr_map = {key: value for key, value in attrs if value is not None}
        attr_names = {key for key, _ in attrs}
        if tag == "input" and attr_map.get("type") == "submit":
            return
        self.fields.append(
            {
                "tag": tag,
                "name": attr_map.get("name"),
                "required": "required" in attr_names,
            }
        )


def _load_json_contract() -> dict[str, dict[str, object]]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _load_js_contract() -> dict[str, dict[str, object]]:
    text = JS_CONTRACT_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"globalThis\.JOBRIGHT_STUB_GAP_CONTRACT\s*=\s*(\{.*\})\s*;",
        text,
        re.DOTALL,
    )
    assert match is not None, "stub_gap_contract.js must export JOBRIGHT_STUB_GAP_CONTRACT"
    return json.loads(match.group(1))


def _parse_form_fields(html: str) -> list[dict[str, object]]:
    parser = _FormFieldParser()
    parser.feed(html)
    return parser.fields


def _field_key(field: dict[str, object]) -> str:
    name = field["name"]
    assert isinstance(name, str)
    return name


def _simulate_autofill(html: str, contract: dict[str, object]) -> tuple[dict[str, str], int]:
    partial_values = contract["partial_values"]
    assert isinstance(partial_values, dict)

    required_gap = contract["required_input_left_empty"]
    textarea_gap = contract["textarea_left_empty"]
    assert isinstance(required_gap, str)
    assert isinstance(textarea_gap, str)

    values: dict[str, str] = {}
    for field in _parse_form_fields(html):
        key = _field_key(field)
        tag = field["tag"]
        if key == required_gap and tag == "input":
            continue
        if key == textarea_gap and tag == "textarea":
            continue
        if key in partial_values:
            values[key] = str(partial_values[key])

    empty_required = 0
    for field in _parse_form_fields(html):
        key = _field_key(field)
        if not field["required"]:
            continue
        value = values.get(key, "")
        if not value.strip():
            empty_required += 1

    return values, empty_required


def test_gap_contract_covers_all_ats_fixtures() -> None:
    contract = _load_json_contract()
    assert set(contract) == set(ATS_FIXTURES)


@pytest.mark.parametrize("slug", ATS_FIXTURES)
def test_gap_contract_matches_intentional_gap_names(slug: str) -> None:
    contract = _load_json_contract()[slug]
    assert contract["required_input_left_empty"] == INTENTIONAL_REQUIRED_GAPS[slug]
    assert contract["textarea_left_empty"] == INTENTIONAL_TEXTAREA_GAPS[slug]


def test_js_gap_contract_matches_json_source() -> None:
    assert _load_js_contract() == _load_json_contract()


def test_manifest_loads_gap_contract_before_content_script() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    scripts = manifest["content_scripts"][0]["js"]
    assert scripts.index("stub_gap_contract.js") < scripts.index("content.js")


@pytest.mark.parametrize("slug", ATS_FIXTURES)
def test_ats_html_matches_gap_contract(slug: str) -> None:
    contract = _load_json_contract()[slug]
    html = (FIXTURES_DIR / "ats" / f"{slug}.html").read_text(encoding="utf-8")
    fields = _parse_form_fields(html)
    names = {_field_key(field) for field in fields}

    required_gap = contract["required_input_left_empty"]
    textarea_gap = contract["textarea_left_empty"]
    assert isinstance(required_gap, str)
    assert isinstance(textarea_gap, str)
    assert required_gap in names
    assert textarea_gap in names

    required_gap_fields = [
        field for field in fields if _field_key(field) == required_gap
    ]
    assert len(required_gap_fields) == 1
    assert required_gap_fields[0]["tag"] == "input"
    assert required_gap_fields[0]["required"] is True

    textarea_fields = [field for field in fields if _field_key(field) == textarea_gap]
    assert len(textarea_fields) == 1
    assert textarea_fields[0]["tag"] == "textarea"


@pytest.mark.parametrize("slug", ATS_FIXTURES)
def test_partial_values_do_not_include_intentional_gaps(slug: str) -> None:
    contract = _load_json_contract()[slug]
    partial_values = contract["partial_values"]
    assert isinstance(partial_values, dict)
    assert contract["required_input_left_empty"] not in partial_values
    assert contract["textarea_left_empty"] not in partial_values


@pytest.mark.parametrize("slug", ATS_FIXTURES)
def test_simulated_autofill_leaves_exactly_one_required_input_empty(slug: str) -> None:
    contract = _load_json_contract()[slug]
    html = (FIXTURES_DIR / "ats" / f"{slug}.html").read_text(encoding="utf-8")
    values, empty_required = _simulate_autofill(html, contract)

    required_gap = contract["required_input_left_empty"]
    textarea_gap = contract["textarea_left_empty"]
    assert isinstance(required_gap, str)
    assert isinstance(textarea_gap, str)

    assert required_gap not in values
    assert textarea_gap not in values
    assert empty_required == 1


def test_content_js_uses_shared_gap_contract() -> None:
    content = CONTENT_JS_PATH.read_text(encoding="utf-8")
    assert "JOBRIGHT_STUB_GAP_CONTRACT" in content
    assert "PARTIAL_VALUES" not in content
    assert "REQUIRED_INPUT_LEFT_EMPTY" not in content
    assert "remainingRequired: 2" not in content
    assert "countEmptyRequiredControls" in content
