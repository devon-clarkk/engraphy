"""Fill `similarity: null` in fixtures/dedup_cases.yaml using the pinned embedding
model. Run in E1 for the original pairs; re-run to baseline NEWLY-ADDED null pairs
(existing non-null values are left untouched -- the pin is ±0.02 forever).

Both sides of every pair are embedded with the `search_document:` task prefix via
embed_document() -- dedup compares document-vs-document (QUESTIONS.md
embedding-task-prefix). The pin is ±0.02 forever, so the prefix decision landed
before this ran.

Surfaces (Phase C, fact-searchability-phase-c.md §3.2):
  --surface old  (default): embed title + "\n" + body -- the pre-Phase-C surface.
  --surface new           : embed searchable_text(title, body, render_attr_surface(
                            attrs)) -- attrs' searchable keys (string/date) enter
                            the document. Used to MEASURE the shift against the
                            old-surface pinned before-values; writes to the
                            fixture only under --rebaseline (§3.3's report holds
                            the after-values).

Flags:
  (default)      fill only pairs whose similarity is null; never overwrite.
  --rebaseline   recompute EVERY pair on the chosen surface and rewrite (a
                 deliberate model/surface change -- design/04 playbook).

Prints old|new comparison rows when --surface new; on --surface old fills nulls.
A computed band that disagrees with the case's hand-authored expect_band is NOT
nudged -- it is a QUESTIONS.md entry (IMPLEMENTER.md); the script exits non-zero.
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engraphy.core import embedding  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "engraphy/tests/fixtures/dedup_cases.yaml"
T_HIGH, T_LOW = 0.95, 0.80


def _band(similarity: float) -> str:
    if similarity >= T_HIGH:
        return "merge"
    if similarity >= T_LOW:
        return "pending"
    return "insert"


def _text(side: dict, surface: str) -> str:
    """The embedded document for one side, per surface. OLD = title+body (07
    §Exact formulas); NEW = searchable_text with the side's searchable attrs
    rendered in (Phase C). The fixture pairs carry only construct-searchable
    attrs (string/date), so every declared attr renders."""
    title, body = side["title"], side["body"]
    if surface == "old":
        return title + "\n" + body
    attrs = side.get("attrs") or {}
    extra = embedding.render_attr_surface(attrs, set(attrs))
    return embedding.searchable_text(title, body, extra)


def _sim(case: dict, surface: str) -> float:
    va = embedding.embed_document(_text(case["a"], surface))
    vb = embedding.embed_document(_text(case["b"], surface))
    return round(sum(x * y for x, y in zip(va, vb)), 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--surface", choices=("old", "new"), default="old")
    ap.add_argument("--rebaseline", action="store_true")
    args = ap.parse_args()

    cases = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    to_fill = [c for c in cases if c.get("similarity") is None]

    if args.surface == "new" and not args.rebaseline:
        # Report-only: measure the new-surface shift vs the pinned old-surface
        # values; write nothing (the recalibration report holds the after-values).
        embedding.load_model()
        print(f"{'case':42s} {'old':>8s} {'new':>8s} {'delta':>8s}  band_old -> band_new")
        crossings = []
        for case in cases:
            old = case.get("similarity")
            new = _sim(case, "new")
            bo = _band(old) if old is not None else "?"
            bn = _band(new)
            d = (new - old) if old is not None else 0.0
            flag = ""
            # class-level crossing (§3.3): restatement pair dropping below t_high,
            # or a distinct pair rising above t_low. Named by expect_band class.
            if case["expect_band"] == "merge" and old is not None and old >= T_HIGH and new < T_HIGH:
                flag = "  <-- CROSSING: restatement fell below t_high"
                crossings.append(case["name"])
            if case["expect_band"] == "insert" and old is not None and old < T_LOW and new >= T_LOW:
                flag = "  <-- CROSSING: distinct rose above t_low"
                crossings.append(case["name"])
            print(f"{case['name']:42s} {(f'{old:.4f}' if old is not None else 'NA'):>8s} "
                  f"{new:8.4f} {d:+8.4f}  {bo} -> {bn}{flag}")
        if crossings:
            print(f"\n{len(crossings)} CLASS-LEVEL BAND CROSSING(S): {crossings}")
            print("STOP -- surface this fixture table to Devon (QUESTIONS.md); do NOT nudge thresholds.")
            return 2
        print("\nNo class-level band crossing. Shipped defaults (0.95/0.80) stand (§3.3).")
        return 0

    targets = cases if args.rebaseline else to_fill
    if not targets:
        print("nothing to baseline (no null pairs; pass --rebaseline to recompute all).")
        return 0

    embedding.load_model()
    fill = {}
    mismatches = []
    for case in targets:
        sim = _sim(case, args.surface)
        fill[case["name"]] = sim
        got = _band(sim)
        flag = "" if got == case["expect_band"] else f"  <-- MISMATCH (fixture says {case['expect_band']})"
        if flag:
            mismatches.append((case["name"], case["expect_band"], got, sim))
        print(f"{case['name']:42s} sim={sim:.4f}  band={got}{flag}")

    text = FIXTURE.read_text(encoding="utf-8")
    if args.rebaseline:
        # rewrite every case's similarity line, matched by preceding `- name:`.
        for case in cases:
            text = re.sub(
                rf"(- name: {re.escape(case['name'])}\b(?:.|\n)*?\n\s*similarity:\s*)\S+",
                lambda m, v=fill[case["name"]]: f"{m.group(1)}{v:.4f}",
                text, count=1,
            )
        n = len(cases)
    else:
        it = iter(fill[c["name"]] for c in to_fill)
        text, n = re.subn(
            r"(?m)^(?P<indent>\s*)similarity:\s*null\b.*$",
            lambda m: f"{m.group('indent')}similarity: {next(it):.4f}",
            text,
        )
        if n != len(to_fill):
            print(f"ERROR: replaced {n} null lines but had {len(to_fill)} null pairs -- aborting")
            return 1
    FIXTURE.write_text(text, encoding="utf-8")
    print(f"\nwrote {n} similarities to {FIXTURE.name} (surface={args.surface})")

    if mismatches:
        print(f"\n{len(mismatches)} band mismatch(es) -- the real model disagrees with a hand-authored")
        print("expect_band. Per IMPLEMENTER.md this is a QUESTIONS.md entry, NOT a fixture/threshold nudge.")
        return 2
    print("\nall bands consistent with the default 0.95/0.80 thresholds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
