"""engram_validate_attrs() (plpgsql) — design/implementation/attr-spec-interpreter-plan.md
§Test plan, row `test_attr_spec_pg.py`: every fixture case in
fixtures/attr_spec_cases.yaml, exact ordered-array match, via raw SQL
(`SELECT engram_validate_attrs(...)`) against a live Postgres — this is the
authority; `test_attr_spec_py.py` is the mirror. Requires
ENGRAPHY_TEST_DATABASE_URL (defaults to the IMPLEMENTER.md scratch instance).
"""

import json
import os
import pathlib

import psycopg
import pytest
import yaml

FIXTURES_PATH = pathlib.Path(__file__).parent / "fixtures" / "attr_spec_cases.yaml"
CASES = yaml.safe_load(FIXTURES_PATH.read_text(encoding="utf-8"))

DATABASE_URL = os.environ.get(
    "ENGRAPHY_TEST_DATABASE_URL",
    "postgres://postgres:engram@localhost:5432/engram_dev?sslmode=disable",
)


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DATABASE_URL) as c:
        yield c


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_case(conn, case):
    spec_json = json.dumps(case["spec"])
    attrs_json = json.dumps(case["attrs"])
    with conn.cursor() as cur:
        cur.execute("SELECT engram_validate_attrs(%s::jsonb, %s::jsonb)", (spec_json, attrs_json))
        (result,) = cur.fetchone()
    assert list(result) == case["expect"]
