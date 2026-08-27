"""The dedup fixtures, asserted against the embedding profile that is actually running.

One file per vector space. `dedup_cases.yaml` pins fp32,
`dedup_cases_onnx_int8.yaml` pins int8, `dedup_cases_micro.yaml` pins gte-small.
Every file carries the SAME cases and the SAME expected bands, because the band a
pair lands in is design intent and does not change when the embedder does. Only
the measured similarity differs, and the calibrated thresholds move with it.

Two assertions, and the first is the one that matters:

**The band is the contract.** Whatever profile is active, every fixture must select
the band its case declares. This is what "int8 is calibrated" means operationally,
and it is what would have caught the calibration being carried over unchanged from
the fp32 space (it does not survive: one pair drifts up across `t_low`, which is
why int8 runs 0.81 rather than 0.80).

**The similarity is pinned within the file's own +/-0.02.** Softer, and it is
allowed to be: quantized arithmetic moves slightly with the runtime build, so this
catches a changed model or a changed pipeline, not float weather.
"""
import os
from pathlib import Path

import pytest
import yaml

from engraphy.core import embedding
from engraphy.core.dedup import BandThresholds, select_band

FIXTURES = Path(__file__).parent / "fixtures"
TOLERANCE = 0.02

_PROFILE_FIXTURES = {
    "legacy-torch": "dedup_cases.yaml",
    "onnx-fp32": "dedup_cases.yaml",
    "onnx-int8": "dedup_cases_onnx_int8.yaml",
    "micro": "dedup_cases_micro.yaml",
}

#: Profiles whose similarities are pinned in their own file rather than shared
#: with the fp32 space. These are the ones a calibration exists for, and the ones
#: the parity and calibration assertions below iterate.
_CALIBRATED = ("onnx-int8", "micro")


def _cases(profile: str):
    path = FIXTURES / _PROFILE_FIXTURES[profile]
    if not path.exists():
        pytest.skip(f"no committed fixture for {profile}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _source_cases():
    return yaml.safe_load((FIXTURES / "dedup_cases.yaml").read_text(encoding="utf-8"))


def _embed_pair(profile: str, case: dict):
    src = {c["name"]: c for c in _source_cases()}[case["name"]]
    out = []
    for side in ("a", "b"):
        text = embedding.searchable_text(src[side]["title"], src[side]["body"], "")
        out.append(embedding.embed_with(
            profile, embedding.document_prefix(profile) + text))
    return sum(x * y for x, y in zip(out[0], out[1]))


@pytest.mark.parametrize("profile", _CALIBRATED)
def test_every_fixture_file_describes_the_same_cases(profile):
    """If a file drifted apart in content it would silently stop being a
    translation of the contract and start being a second, weaker one."""
    source = [c["name"] for c in _source_cases()]
    assert [c["name"] for c in _cases(profile)] == source
    src_bands = {c["name"]: c["expect_band"] for c in _source_cases()}
    for case in _cases(profile):
        assert case["expect_band"] == src_bands[case["name"]], (
            f"{case['name']}: the expected band must be the same on every profile; "
            f"a band is design intent, not a property of the embedder")


@pytest.mark.parametrize("profile", list(_PROFILE_FIXTURES))
def test_pinned_similarities_select_the_expected_band(profile):
    """Pure arithmetic over the committed numbers: no model loads. Proves the
    profile's band defaults and its pinned similarities agree with each other."""
    bands = BandThresholds.for_profile(profile)
    for case in _cases(profile):
        got = select_band(case["similarity"], bands)
        assert got == case["expect_band"], (
            f"[{profile}] {case['name']}: similarity {case['similarity']} selects "
            f"{got} at t_high={bands.t_high}/t_low={bands.t_low}, "
            f"expected {case['expect_band']}")


#: The int8 pins are hardware-dependent (see `_PROFILE_BANDS` in core/dedup.py):
#: the same code on two Linux x86-64 hosts put the confirm-edge fixtures 0.026
#: apart, which is past the file's own tolerance. Asserting them on arbitrary CI
#: hardware would test the runner, not the engine, so the live int8 checks run
#: where the numbers were baselined. Set ENGRAPHY_INT8_FIXTURES=1 to enable them
#: after baselining on that host.
_INT8_LIVE = os.environ.get("ENGRAPHY_INT8_FIXTURES") == "1"
_int8_live = pytest.mark.skipif(
    not _INT8_LIVE,
    reason="int8 fixture pins are host-specific; baseline on this host and set "
           "ENGRAPHY_INT8_FIXTURES=1")


@_int8_live
def test_live_embeddings_reproduce_the_pinned_similarities_int8():
    """Catches a changed model, revision, or pooling step on the profile this
    release calibrates.

    Scoped to int8 on purpose. The fp32 pins in `dedup_cases.yaml` are E1-era, and
    one of them (`unrelated_insert`, pinned 0.4196) now measures 0.4481 on BOTH
    fp32 profiles, torch included. That is a pre-existing property of the
    committed file rather than anything the ONNX work changed, and the file's own
    header restricts re-baselining to a deliberate model change, which this is
    not. So the pin is asserted where it was freshly derived, and the fp32 file is
    held to its band contract below, which it satisfies.
    """
    for case in _cases("onnx-int8"):
        sim = _embed_pair("onnx-int8", case)
        assert abs(sim - case["similarity"]) <= TOLERANCE, (
            f"[onnx-int8] {case['name']}: measured {sim:.4f} against pinned "
            f"{case['similarity']:.4f}, beyond the +/-{TOLERANCE} the fixture allows")


@_int8_live
def test_live_embeddings_select_the_expected_band_int8():
    """The contract itself, end to end: real vectors, int8's own bands, the band
    each case declares. This is what "calibrated" has to mean, and it is the
    assertion that fails if the profile ships on the wrong band pair.

    Scoped to int8, and the reason is worth recording rather than leaving as an
    unexplained omission. Two of the E1-era pins in `dedup_cases.yaml` no longer
    describe what the fp32 pipeline measures today, on torch and ONNX alike:

      unrelated_insert                     pinned 0.4196, measures 0.4481
      boundary_hunt_near_t_low_insert_side pinned 0.7838, measures 0.8021

    The second is the interesting one. It is still inside the file's own +/-0.02,
    yet it crosses `t_low` = 0.80, so the pin can be satisfied while the band it
    was written to demonstrate flips. That is a property of where the case sits
    relative to the edge, and it predates this work: neither value moves when the
    executor changes, and no test read this file before now, which is why it went
    unnoticed.

    Re-baselining is out of scope here. The file's header allows it only on a
    deliberate model change, and this release does not change the fp32 model; the
    fp32 space is byte-for-byte what it was. Flagged for a follow-up rather than
    quietly rewritten to make a new test green.
    """
    bands = BandThresholds.for_profile("onnx-int8")
    for case in _cases("onnx-int8"):
        sim = _embed_pair("onnx-int8", case)
        got = select_band(sim, bands)
        assert got == case["expect_band"], (
            f"[onnx-int8] {case['name']}: live similarity {sim:.4f} selects {got} "
            f"at t_high={bands.t_high}/t_low={bands.t_low}, "
            f"expected {case['expect_band']}")


@pytest.mark.parametrize("profile", _CALIBRATED)
def test_calibration_is_required_not_cosmetic(profile):
    """The per-profile bands are not decoration: running a calibrated profile on
    the fp32 pair actually mis-bands committed fixtures. Asserting that keeps the
    calibration from being "simplified" away by someone who assumes the profiles
    are interchangeable."""
    fp32_bands = BandThresholds.for_profile("onnx-fp32")
    wrong = [c["name"] for c in _cases(profile)
             if select_band(c["similarity"], fp32_bands) != c["expect_band"]]
    assert wrong, (f"{profile} similarities band identically under the fp32 "
                   f"thresholds; if that is genuinely true now, its _PROFILE_BANDS "
                   f"entry can be dropped")


@pytest.mark.parametrize("profile", _CALIBRATED)
def test_calibrated_bands_are_not_the_fp32_bands(profile):
    """Guards each calibration against being quietly reverted. If someone
    simplified `_PROFILE_BANDS` away, every other test here would still pass on
    the fp32 profile and the calibrated profiles would start mis-banding in
    production.

    The assertion is inequality, not a direction. int8 runs BELOW the fp32
    `t_high` because quantization contracts pairwise cosine; `micro` runs ABOVE it
    because gte-small scores every pair higher and packs them into a narrower
    range. Asserting a direction would encode one model's behaviour as a rule."""
    fp32 = BandThresholds.for_profile("onnx-fp32")
    calibrated = BandThresholds.for_profile(profile)
    assert (calibrated.t_high, calibrated.t_low) != (fp32.t_high, fp32.t_low)


def test_every_profile_with_its_own_fixture_file_has_its_own_bands():
    """A profile that pins its own similarities is by definition in its own
    vector space, so it must also carry its own calibration. The reverse gap is
    the dangerous one -- shipping a new profile's fixtures while it silently runs
    the fp32 defaults -- and this is what closes it."""
    from engraphy.core.dedup import _PROFILE_BANDS

    for profile in _CALIBRATED:
        assert profile in _PROFILE_BANDS, (
            f"{profile} pins its own dedup fixtures but has no _PROFILE_BANDS "
            f"entry, so it would run the fp32 thresholds against a different "
            f"vector space")
