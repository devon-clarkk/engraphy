"""Parity fuzzer — design/implementation/attr-spec-interpreter-plan.md §Test plan,
row `test_attr_spec_parity.py`: 2,000 generated (spec, attrs) pairs covering the
attr-spec grammar; plpgsql (`engram_validate_attrs`) and Python
(`engraphy.core.attr_spec.validate_attrs`) outputs must be identical. Requires a
live Postgres (ENGRAPHY_TEST_DATABASE_URL, defaults to the scratch instance).
"""

import json
import os

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engraphy.core.attr_spec import validate_attrs

DATABASE_URL = os.environ.get(
    "ENGRAPHY_TEST_DATABASE_URL",
    "postgres://postgres:engram@localhost:5432/engram_dev?sslmode=disable",
)

# `addenda` is in the pool deliberately: it is the one engine-reserved key
# (engraphy.core.attr_spec.RESERVED_ATTR_KEYS), exempt from the Phase-3
# closed-spec check on BOTH sides (migration 0017 / attr_spec.py). The
# exemption is only trustworthy if the fuzzer can actually generate it --
# before migration 0017 the two implementations agreed *because* neither had
# the exemption, and this pool's omission of the key is why the divergence
# introduced by fixing one side alone would have gone unnoticed.
KEYS = ["addenda", "alpha", "beta", "gamma", "delta", "epsilon"]
TYPES = ["string", "int", "number", "bool", "date"]
ENUM_POOL = ["a", "b", "c", "d", "yes", "no", "true", "open", "closed", "command"]

# Phase C: `searchable` is an optional rule-object key (fact-searchability-phase-c.md
# §1). Neither interpreter acts on it -- both read only type/enum/required/optional/
# closed/requires -- so both must simply TOLERATE it. Generate it on ~half of rule
# objects (the 0017 lesson: a key the fuzzer never generates is a key the two sides
# can only agree on by both being wrong). If either side ever starts rejecting
# unknown rule keys, this fuzzer catches the divergence.
_rule_strategy = st.one_of(
    st.sampled_from(TYPES).map(lambda t: {"type": t}),
    st.lists(st.sampled_from(ENUM_POOL), min_size=1, max_size=4, unique=True).map(
        lambda vs: {"enum": vs}
    ),
).flatmap(
    lambda base: st.one_of(
        st.just(base),
        st.booleans().map(lambda flag: {**base, "searchable": flag}),
    )
)

_value_strategy = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1000, max_value=1000),
    st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
    st.sampled_from(
        [
            "a",
            "b",
            "yes",
            "no",
            "command",
            "2026-01-15",
            "2026-02-30",  # regex-valid, cast-invalid
            "06/07/2026",  # regex-invalid
            "x" * 2000,
            "x" * 2001,
            "",
        ]
    ),
    st.lists(st.integers(min_value=-10, max_value=10), max_size=3),
    st.dictionaries(st.sampled_from(["x", "y"]), st.integers(min_value=-10, max_value=10), max_size=2),
)


@st.composite
def _spec_and_attrs(draw):
    order = draw(st.permutations(KEYS))
    n_req = draw(st.integers(min_value=0, max_value=len(order)))
    req_keys, rest = order[:n_req], order[n_req:]
    n_opt = draw(st.integers(min_value=0, max_value=len(rest)))
    opt_keys = rest[:n_opt]

    req = {k: draw(_rule_strategy) for k in req_keys}
    opt = {k: draw(_rule_strategy) for k in opt_keys}
    closed = draw(st.one_of(st.none(), st.booleans()))

    n_cond = draw(st.integers(min_value=0, max_value=2))
    requires = []
    for _ in range(n_cond):
        requires.append(
            {
                "key": draw(st.sampled_from(KEYS)),
                "when": {
                    "key": draw(st.sampled_from(KEYS)),
                    "equals": draw(st.sampled_from(ENUM_POOL)),
                },
            }
        )

    attrs_section = {}
    if req:
        attrs_section["required"] = req
    if opt:
        attrs_section["optional"] = opt
    if closed is not None:
        attrs_section["closed"] = closed
    if requires:
        attrs_section["requires"] = requires
    spec = {"attrs": attrs_section} if attrs_section else {}

    attrs = {}
    for k in KEYS:
        if draw(st.booleans()):
            attrs[k] = draw(_value_strategy)

    return spec, attrs


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DATABASE_URL) as c:
        yield c


@settings(max_examples=2000, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(pair=_spec_and_attrs())
def test_parity(conn, pair):
    spec, attrs = pair
    py_errors = validate_attrs(spec, attrs)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT engram_validate_attrs(%s::jsonb, %s::jsonb)",
            (json.dumps(spec), json.dumps(attrs)),
        )
        (pg_errors,) = cur.fetchone()
    assert py_errors == list(pg_errors), (spec, attrs, py_errors, list(pg_errors))
