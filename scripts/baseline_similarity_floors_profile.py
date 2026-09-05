"""Baseline the two ABSOLUTE similarity floors for a non-default embedding profile.

`dedup.t_high` and `dedup.t_low` are not the only cosines this engine compares a
literal against. Two more decide what a reader sees:

  `briefing.semantic_floor`  0.50 on the fp32 space. A node whose hint<->document
                             similarity falls below it is dropped from a semantic
                             briefing section.
  `resonance.floor`          0.75 on the fp32 space. A node below it never appears
                             in a resonance report.

Both are absolute values on a scale that belongs to the MODEL, not to the engine,
so a profile running different weights inherits them wrongly. gte-small scores a
clearly-unrelated pair around 0.71 where nomic scores it around 0.45: carried
over unchanged, the fp32 floors would sit under every node in the store, and the
visible effect is a briefing whose "relevant" section is merely "everything" and
a resonance report that resonates with anything. Nothing errors. That is the
silent read-path change the dedup band calibration exists to prevent, arriving
through a different door.

    python scripts/baseline_similarity_floors_profile.py --profile micro

## What each half is worth, stated plainly

**The semantic floor is measured against labels.**
`fixtures/briefing/section_cases.yaml` carries a case
(`semantic_with_hint_membership`) that declares, for one real hint, which nodes
belong in the section and which do not. That is the same kind of labelled data
the dedup fixtures are, so the same exact-arithmetic window applies: the floor
must sit above every excluded node and at or below every member.

**The resonance floor is a parity derivation, and that is weaker.** No fixture in
this repo labels a resonance decision, so there is no window to take. What can be
computed is the value that makes the new profile treat the 17 dedup pairs exactly
as the fp32 space treats them at 0.75: the same pairs above the floor, the same
pairs below it. That preserves behaviour, which is what an operator adopting a
profile actually needs; it does not establish that 0.75 was the right answer on
fp32 in the first place, and this script does not claim it does.

The bare insert/pending labels are NOT a bound on this floor, and the fp32 run
shows why: 0.75 sits below the lowest same-topic pair and below two pairs the
fixtures band `insert`. Resonance is deliberately looser than dedup. It asks
whether a node is worth mentioning, not whether it is the same fact.

Both halves report a WINDOW and its width. Width is the point: a wide window
means a shipped default survives a different host, a narrow one means the value
has to be calibrated per deployment. Both floors are per-space config keys
(`briefing.semantic_floor`, `resonance.floor`), so the operator has that lever.
"""
import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engraphy.core import embedding
from engraphy.core.dedup import RESONANCE_FLOOR

ROOT = Path(__file__).resolve().parents[1]
DEDUP_CASES = ROOT / "engraphy/tests/fixtures/dedup_cases.yaml"
BRIEFING_CASES = ROOT / "engraphy/tests/fixtures/briefing/section_cases.yaml"
SEMANTIC_CASE = "semantic_with_hint_membership"

#: The fp32 reference profile every parity computation is taken against. It is
#: the space every shipped default was chosen on and the one bit-reproducible
#: across hosts, so it is the only defensible thing to hold a new profile to.
REFERENCE = "onnx-fp32"


def _cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _pair_similarity(profile: str, case: dict) -> float:
    prefix = embedding.document_prefix(profile)
    vecs = [embedding.embed_with(
        profile, prefix + embedding.searchable_text(case[side]["title"], case[side]["body"], ""))
        for side in ("a", "b")]
    return round(_cos(*vecs), 4)


def _window(below, at_or_above, label):
    """(lo, hi]: `lo` exclusive, the highest similarity that must fall BELOW this
    floor; `hi` inclusive, the lowest that must sit on or above it. The bounding
    items are printed too, because knowing WHICH one pins an edge is what tells
    an operator whether a near miss is structural or incidental."""
    lo, lo_by = max(below) if below else (0.0, "-")
    hi, hi_by = min(at_or_above) if at_or_above else (1.0, "-")
    print(f"\n  {label:24} ({lo:.4f}, {hi:.4f}]   width {hi - lo:+.4f}", file=sys.stderr)
    print(f"  {'':24} lower edge {lo_by}, upper edge {hi_by}", file=sys.stderr)
    if hi <= lo:
        print(f"  {'':24} NO VIABLE VALUE: those two cannot both be satisfied",
              file=sys.stderr)
        return None
    print(f"  {'':24} midpoint {round((lo + hi) / 2, 4)}", file=sys.stderr)
    return lo, hi


def semantic_floor(profile: str):
    """MEASURED. The window the labelled briefing case admits on this profile."""
    cases = yaml.safe_load(BRIEFING_CASES.read_text(encoding="utf-8"))
    case = next(c for c in cases if c["name"] == SEMANTIC_CASE)
    section = case["briefing"]["sections"][0]
    expect = case["expect"]["sections"][0]
    # A section filters by type BEFORE the floor applies, so a node excluded for
    # its type says nothing about where the floor belongs and is skipped here.
    eligible = [n for n in case["seed"]["nodes"] if n["type"] in section["types"]]

    query = embedding.embed_with(profile, embedding.query_prefix(profile) + case["hint"])
    members, excluded = [], []
    print(f"\nbriefing.semantic_floor -- measured against {SEMANTIC_CASE}:", file=sys.stderr)
    for node in eligible:
        text = embedding.searchable_text(node["title"], node["body"], "")
        doc = embedding.embed_with(profile, embedding.document_prefix(profile) + text)
        sim = round(_cos(query, doc), 4)
        keep = node["label"] in expect["members"]
        print(f"  {node['label']:16} {sim:.4f}  {'in section' if keep else 'excluded':<10}"
              f" (fp32 pin {case['hint_similarities'][node['label']]:.4f})", file=sys.stderr)
        (members if keep else excluded).append((sim, node["label"]))
    return _window(excluded, members, "briefing.semantic_floor")


def resonance_floor(profile: str):
    """PARITY-DERIVED. The window that reproduces, on this profile, the exact
    partition of the dedup fixtures that `RESONANCE_FLOOR` produces on the fp32
    reference: every pair fp32 admits to a resonance report admitted here, every
    pair it drops dropped here.

    Both profiles are measured in this one run rather than one being read from a
    pinned file, so the partition is taken on THIS host and the comparison is
    like for like.
    """
    cases = yaml.safe_load(DEDUP_CASES.read_text(encoding="utf-8"))
    admitted, dropped = [], []
    print(f"\nresonance.floor -- parity against {REFERENCE} at {RESONANCE_FLOOR}:",
          file=sys.stderr)
    rows = []
    for c in cases:
        reference = _pair_similarity(REFERENCE, c)
        here = _pair_similarity(profile, c)
        rows.append((reference, here, c["name"], c["expect_band"]))
        (admitted if reference >= RESONANCE_FLOOR else dropped).append((here, c["name"]))
    for reference, here, name, band in sorted(rows):
        mark = "admit " if reference >= RESONANCE_FLOOR else "drop  "
        print(f"  {name:44} {REFERENCE} {reference:.4f} -> {here:.4f}  {mark} ({band})",
              file=sys.stderr)
    return _window(dropped, admitted, "resonance.floor")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=list(embedding.PROFILES))
    args = ap.parse_args()

    print(f"profile={args.profile}", file=sys.stderr)
    windows = {
        "briefing.semantic_floor": semantic_floor(args.profile),
        "resonance.floor": resonance_floor(args.profile),
    }

    # Reported, never written. These are code defaults in two different modules
    # and moving one is a reviewed edit, not a script's side effect. The midpoint
    # is the suggestion for the same reason the dedup bands take it: it is the
    # value with the most room on both sides, and across a different vector
    # space there is no other profile's number worth being near.
    print("\nsuggested code defaults for this profile (window midpoints):", file=sys.stderr)
    for label, window in windows.items():
        if window is None:
            print(f"  {label:24} no viable value on this host", file=sys.stderr)
            continue
        lo, hi = window
        print(f"  {label:24} {round((lo + hi) / 2, 2):.2f}"
              f"   from ({lo:.4f}, {hi:.4f}], width {hi - lo:.4f}", file=sys.stderr)
    if any(w is None for w in windows.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
