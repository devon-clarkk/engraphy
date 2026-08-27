"""Baseline the dedup fixtures for a NON-default embedding profile.

`dedup_cases.yaml` pins the fp32 vector space and its header restricts
re-baselining to a deliberate model change. int8 quantization is a deliberate
change of vector space, but it does not replace that space: `legacy-torch` and
`onnx-fp32` still produce those exact similarities, and their pinned values are
still the contract for anyone on those profiles.

So the int8 numbers go in their own file rather than over the top of the existing
one. This script writes it, reading the SAME cases from the same source of truth
so the two files can never drift apart in content, only in measured similarity.

Run it inside the deployment image, not on a developer laptop. Quantized
arithmetic is platform-sensitive in a way fp32 is not, and a value baselined on
one OS can miss the +/-0.02 pin on another.

    python scripts/baseline_dedup_fixtures_profile.py --profile onnx-int8
"""
import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engraphy.core import embedding
from engraphy.core.dedup import BandThresholds, select_band

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "engraphy/tests/fixtures/dedup_cases.yaml"


def out_path(profile: str) -> Path:
    return ROOT / f"engraphy/tests/fixtures/dedup_cases_{profile.replace('-', '_')}.yaml"


def report_windows(measured, cases):
    """The band windows these similarities admit, and which fixture sets each edge.

    This is the number that decides whether a profile can ship a single default
    pair at all, and it is arithmetic rather than a search: `t_high` must sit at
    or below the lowest merge-expected similarity and strictly above the highest
    pending-expected one, and `t_low` stands in the same relation to pending and
    insert. A grid sweep would find the same answer less exactly.

    Width is the point. A wide window means a shipped default survives a
    different CPU; a narrow one means the profile has to be calibrated per host,
    which is what `onnx-int8` learned the expensive way -- it derived a defensible
    pair on one Linux host, and a second Linux host put the confirm-edge fixtures
    0.026 away, outside any window either host admitted. Print the bounding
    fixture names too: knowing WHICH pair pins an edge is what tells an operator
    whether a near-miss is structural or incidental.
    """
    band_of = {c["name"]: c["expect_band"] for c in cases}
    sims = {"merge": [], "pending": [], "insert": []}
    for row in measured:
        sims[band_of[row["name"]]].append((row["similarity"], row["name"]))

    def window(above, below, label):
        # (lo, hi]: lo is exclusive, set by the highest similarity that must fall
        # BELOW this edge; hi is inclusive, set by the lowest that must sit on or
        # above it.
        lo, lo_by = max(above) if above else (0.0, "-")
        hi, hi_by = min(below) if below else (1.0, "-")
        print(f"  {label:7} ({lo:.4f}, {hi:.4f}]  width {hi - lo:+.4f}"
              f"   lower edge set by {lo_by}, upper by {hi_by}", file=sys.stderr)
        if hi <= lo:
            print(f"  {label:7} NO VIABLE VALUE: {lo_by} ({lo:.4f}) and {hi_by} "
                  f"({hi:.4f}) cannot both band correctly", file=sys.stderr)

    print("", file=sys.stderr)
    print("band windows these fixtures admit on this host:", file=sys.stderr)
    window(sims["pending"], sims["merge"], "t_high")
    window(sims["insert"], sims["pending"], "t_low")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=[p for p in embedding.PROFILES])
    ap.add_argument("--t-high", type=float, default=None,
                    help="band to check against; defaults to the profile's own default")
    ap.add_argument("--t-low", type=float, default=None)
    args = ap.parse_args()

    cases = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    prefix = embedding.document_prefix(args.profile)
    bands = BandThresholds(
        t_high=args.t_high if args.t_high is not None else BandThresholds.for_profile(args.profile).t_high,
        t_low=args.t_low if args.t_low is not None else BandThresholds.for_profile(args.profile).t_low)

    print(f"profile={args.profile}  t_high={bands.t_high}  t_low={bands.t_low}", file=sys.stderr)
    out, disagreements = [], []
    for c in cases:
        a = embedding.embed_with(
            args.profile,
            prefix + embedding.searchable_text(c["a"]["title"], c["a"]["body"], ""))
        b = embedding.embed_with(
            args.profile,
            prefix + embedding.searchable_text(c["b"]["title"], c["b"]["body"], ""))
        sim = round(sum(x * y for x, y in zip(a, b)), 4)
        band = select_band(sim, bands)
        if band != c["expect_band"]:
            disagreements.append((c["name"], c["expect_band"], band, sim))
        out.append({"name": c["name"], "expect_band": c["expect_band"], "similarity": sim})
        print(f"  {c['name']:44} {sim:.4f}  {band:<7} (fp32 pin {c['similarity']:.4f})",
              file=sys.stderr)

    report_windows(out, cases)

    if disagreements:
        # Same posture as the fp32 baseline script: a computed band that
        # disagrees with the hand-authored intent is a decision to make, not a
        # number to nudge.
        print("\nBAND DISAGREEMENTS -- do not nudge, decide:", file=sys.stderr)
        for name, want, got, sim in disagreements:
            print(f"  {name}: expected {want}, got {got} at {sim:.4f}", file=sys.stderr)
        sys.exit(1)

    header = (
        f"# Dedup band fixtures for the `{args.profile}` embedding profile.\n"
        f"#\n"
        f"# Companion to dedup_cases.yaml, NOT a replacement: the cases and their\n"
        f"# expected bands are read from that file, which stays the contract for the\n"
        f"# fp32 vector space. Only the measured similarity differs, because int8\n"
        f"# quantization contracts pairwise cosine.\n"
        f"#\n"
        f"# Baselined at t_high={bands.t_high} / t_low={bands.t_low}, inside the\n"
        f"# deployment image. Quantized arithmetic is platform-sensitive, so these\n"
        f"# are measured where the code runs, not on a developer laptop.\n"
        f"# Asserted within the same +/-0.02 the fp32 file uses.\n")
    out_path(args.profile).write_text(
        header + yaml.safe_dump(out, sort_keys=False), encoding="utf-8")
    print(f"\nwrote {out_path(args.profile)}", file=sys.stderr)


if __name__ == "__main__":
    main()
