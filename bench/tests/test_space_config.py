"""Phase A (fact-searchability) — per-space `dedup.t_high` config plumbing.

The engine is unchanged; this exercises the bench-side path that writes a
`config` row via `provision_run_space(space_config=…)` / `_apply_space_config`
and the `--space-config` parser, and proves the engine's own
`dedup._resolve_config` then bands a ~0.96 near-duplicate as PENDING (not MERGE)
at `t_high=0.98`, while ~0.99 still merges. Uses the synthetic controlled-cosine
pattern from engraphy/tests/test_dedup.py; lives under bench/ because nothing in
engraphy/ changed.
"""
from __future__ import annotations

import math

import pytest

from engraphy.core.dedup import write
from engraphy.tests.test_dedup import (  # reuse the exact synthetic-similarity harness
    _bootstrap_write_space,
    _cleanup_write_space,
    _unit_vector_at_angle,
)

from bench.core.run import _parse_space_config
from bench.core.space import _apply_space_config

# controlled cosines to the angle-0 vector
_V0 = _unit_vector_at_angle(0.0)
_V096 = _unit_vector_at_angle(math.acos(0.96))
_V099 = _unit_vector_at_angle(math.acos(0.99))


# ---- pure: the --space-config parser ---------------------------------------

def test_parse_space_config_json_values():
    got = _parse_space_config(["dedup.t_high=0.98", 'x="hi"', "n=3"])
    assert got == {"dedup.t_high": 0.98, "x": "hi", "n": 3}


def test_parse_space_config_empty_is_defaults():
    assert _parse_space_config([]) == {}


def test_parse_space_config_rejects_bad():
    with pytest.raises(SystemExit):
        _parse_space_config(["dedup.t_high"])          # no '='
    with pytest.raises(SystemExit):
        _parse_space_config(["dedup.t_high=0.9x"])     # not JSON


# ---- DB: the config row bands the write ------------------------------------

@pytest.fixture
def cfg_space(conn, request):
    space_id = ("wr-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_write_space(conn, space_id)
    yield space_id, conn
    _cleanup_write_space(conn, space_id)


async def _w(pool, space, title, vec):
    return await write(pool, space, "p1", "widget", "scope1", title,
                       f"body for {title}", {}, vec, "pytest")


async def test_thigh_098_bands_096_pending_099_merge(pool, cfg_space):
    space, conn = cfg_space
    cur = conn.cursor()
    _apply_space_config(cur, space, {"dedup.t_high": 0.98})
    conn.commit()
    # the config row landed
    cur.execute("SELECT value FROM config WHERE space_id=%s AND key='dedup.t_high'", (space,))
    assert cur.fetchone()[0] == 0.98

    assert (await _w(pool, space, "anchor", _V0))["outcome"] == "inserted"
    # 0.96 is now inside [t_low=0.80, t_high=0.98) -> PENDING, not the merge band
    assert (await _w(pool, space, "near-096", _V096))["outcome"] == "needs_confirmation"
    # 0.99 still clears the raised bar -> merge band. Phase B splits that band on
    # novelty: `_w`'s bodies are distinct (derived from the title), so this novel
    # merge lands `merged_linked`, not `merged`. The band, not the split, is what
    # this config test asserts -- so accept either merge-family outcome.
    assert (await _w(pool, space, "near-099", _V099))["outcome"] in ("merged", "merged_linked")


async def test_default_thigh_merges_096(pool, cfg_space):
    """Control: with no config row, the shipped t_high=0.95 still auto-bands the
    same 0.96 pair into the merge band -- so it is the config, not the harness,
    that changed the band. (Phase B: a distinct-content merge-band write lands
    `merged_linked`; the band is the point here, not the novelty split.)"""
    space, conn = cfg_space
    assert (await _w(pool, space, "anchor", _V0))["outcome"] == "inserted"
    assert (await _w(pool, space, "near-096", _V096))["outcome"] in ("merged", "merged_linked")
