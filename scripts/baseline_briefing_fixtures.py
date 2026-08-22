"""One-shot: fill the null `hint_similarities` slots in the
`semantic_with_hint_membership` case of fixtures/briefing/section_cases.yaml,
measured against the pinned model under the +-0.02 protocol (same as
scripts/baseline_dedup_fixtures.py). Run once; re-run only on a deliberate model
change. Refuses to overwrite non-null values without --rebaseline.

Each value is the query<->document cosine: embed_query(hint) . embed_document(
title + "\\n" + body). This is the exact quantity briefing's semantic floor
compares against, so the fixture documents WHY the floor discriminates.

Discrimination guard (QUESTIONS.md "semantic-section-relevance-floor" watch item,
Fable): the floor (0.50) must separate the coffee nodes (>= 0.50) from the
off-topic PR_far (< 0.50). If measurement lands otherwise, this exits non-zero
and prints a QUESTIONS.md-worthy message -- do NOT nudge the fixture.
"""
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engraphy.core import embedding  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "engraphy/tests/fixtures/briefing/section_cases.yaml"
CASE = "semantic_with_hint_membership"
FLOOR = 0.50
COFFEE = ("PR_coffee", "PA_coffee")
OFF_TOPIC = ("PR_far",)


def main() -> int:
    rebaseline = "--rebaseline" in sys.argv
    cases = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    case = next(c for c in cases if c["name"] == CASE)
    node_by_label = {n["label"]: n for n in case["seed"]["nodes"]}
    sims_block = case["hint_similarities"]

    already = [k for k, v in sims_block.items() if v is not None]
    if already and not rebaseline:
        print(f"refusing to overwrite baselined slots {already} without --rebaseline")
        return 1

    embedding.load_model()
    q = embedding.embed_query(case["hint"])
    measured = {}
    for label in sims_block:
        node = node_by_label[label]
        d = embedding.embed_document(node["title"] + "\n" + node["body"])
        measured[label] = round(sum(a * b for a, b in zip(q, d)), 4)
        print(f"{label:12s} query<->doc cosine = {measured[label]:.4f}")

    # Fill the slots by literal text replacement (keeps the file's comments/layout).
    text = FIXTURE.read_text(encoding="utf-8")
    for label, val in measured.items():
        text, n = re.subn(rf"(?m)^(\s*){label}:\s*null\s*$", rf"\g<1>{label}: {val:.4f}", text)
        if n != 1:
            print(f"ERROR: expected exactly one null slot for {label}, replaced {n}")
            return 1
    FIXTURE.write_text(text, encoding="utf-8")
    print(f"\nwrote hint_similarities to {FIXTURE.name}")

    bad = [c for c in COFFEE if measured[c] < FLOOR] + [o for o in OFF_TOPIC if measured[o] >= FLOOR]
    if bad:
        print(f"\nDISCRIMINATION FAILURE at floor {FLOOR}: {[(b, measured[b]) for b in bad]}")
        print("The 0.50 default does not separate on-topic from off-topic on real data.")
        print("Per IMPLEMENTER.md / the resolution's watch item: file a QUESTIONS.md entry for a")
        print("calibrated value -- do NOT nudge the fixture. Values were written (ground truth).")
        return 2
    print(f"\ndiscriminates: coffee {[measured[c] for c in COFFEE]} >= {FLOOR} > PR_far {measured['PR_far']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
