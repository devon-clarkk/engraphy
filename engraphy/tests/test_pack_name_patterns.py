"""engraphy.admin.packs.validate_name_patterns — design/07 §Pack file schema,
QUESTIONS.md 'pack-schema' (resolved): node_types/edge_types/attrmap keys are
NOT constrained by packs/schema.json's patternProperties alone (no
additionalProperties:false at those three levels), so pack validate adds this
explicit check on top of jsonschema. Fixtures:
engraphy/tests/fixtures/packs/MANIFEST.yaml, `name_pattern_violation: true` rows.
"""

import pathlib

import pytest
import yaml

from engraphy.admin.packs import validate_name_patterns

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "packs"
MANIFEST = yaml.safe_load((FIXTURES_DIR / "MANIFEST.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("entry", MANIFEST, ids=[e["file"] for e in MANIFEST])
def test_name_patterns_per_manifest(entry):
    pack = yaml.safe_load((FIXTURES_DIR / entry["file"]).read_text(encoding="utf-8"))
    errors = validate_name_patterns(pack)
    if entry.get("name_pattern_violation"):
        assert errors, f"{entry['file']} should violate a name pattern"
    else:
        assert errors == [], f"{entry['file']} should have no name-pattern errors, got {errors}"


def test_both_shipped_packs_conform():
    repo_root = pathlib.Path(__file__).parents[2]
    packs = [
        repo_root / "packs" / "starter" / "pack.yaml",
        FIXTURES_DIR / "example-pack.yaml",
    ]
    for path in packs:
        pack = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert validate_name_patterns(pack) == [], f"{path.name} should conform"
