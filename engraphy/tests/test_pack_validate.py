"""engraphy.admin.packs.validate() — design/07 §Pack file schema (JSON Schema +
cross-reference validation) and QUESTIONS.md "pack-schema" (name-pattern
check). Runs the full validate() pipeline against every fixture in
fixtures/packs/MANIFEST.yaml: `valid: true` entries must return [], everything
else must return a non-empty error list. Also both shipped packs.
"""

import pathlib

import pytest
import yaml

from engraphy.admin.packs import validate

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "packs"
MANIFEST = yaml.safe_load((FIXTURES_DIR / "MANIFEST.yaml").read_text(encoding="utf-8"))
REPO_ROOT = pathlib.Path(__file__).parents[2]


@pytest.mark.parametrize("entry", MANIFEST, ids=[e["file"] for e in MANIFEST])
def test_validate_per_manifest(entry):
    pack = yaml.safe_load((FIXTURES_DIR / entry["file"]).read_text(encoding="utf-8"))
    errors = validate(pack)
    if entry["valid"]:
        assert errors == [], f"{entry['file']} should be valid, got {errors}"
    else:
        assert errors != [], f"{entry['file']} should be invalid"


def test_crossref_errors_are_crossref_category():
    # Sanity that the crossref fixtures fail for the crossref reason, not
    # merely "some error occurred" -- schema errors would short-circuit and
    # never reach the crossref checks at all.
    cases = {
        "invalid-crossref-edge-rule-unknown-src-type.yaml": "edge_rules[0]: src",
        "invalid-crossref-edge-rule-unknown-edge-type.yaml": "edge_rules[0]: type",
        "invalid-crossref-briefing-unknown-type.yaml": "briefing.sections[0]: type",
        "invalid-crossref-alias-binds-unknown-tool.yaml": "tool_aliases.nuke: binds",
    }
    for filename, expected_prefix in cases.items():
        pack = yaml.safe_load((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
        errors = validate(pack)
        assert any(e.startswith(expected_prefix) for e in errors), (filename, errors)


def test_starter_pack_valid():
    pack = yaml.safe_load((REPO_ROOT / "packs" / "starter" / "pack.yaml").read_text(encoding="utf-8"))
    assert validate(pack) == []


def test_example_pack_valid():
    pack = yaml.safe_load((FIXTURES_DIR / "example-pack.yaml").read_text(encoding="utf-8"))
    assert validate(pack) == []
