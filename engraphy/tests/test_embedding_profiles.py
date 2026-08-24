"""The embedder seam: profile selection, the stamp, and cross-profile parity.

Two things are asserted here and they carry different weight.

**`onnx-fp32` reproduces `legacy-torch`.** This is what makes the runtime swap a
swap rather than a change: the same weights through a different executor, landing
on the same vector. Asserted as a tolerance, not bitwise equality, because that is
what was measured and what is true. Two float pipelines over the same graph agree
to float noise, not to the last bit.

**`onnx-int8` does NOT reproduce it, and by a known amount.** Quantization
contracts pairwise cosine systematically. That is asserted too, as a bounded
disagreement, so the day it stops being bounded the suite says so instead of a
store quietly re-banding.

The parity tests need both backends importable. torch lives in the `legacy-torch`
extra, which `[dev]` pulls in precisely so this stays runnable in CI; where it is
genuinely absent the parity tests skip rather than fail, and the profile/stamp
tests below still run.
"""
import math
import os

import pytest

from engraphy.core import embedding

# Short, varied, and including a near-identical pair, which is where a quantized
# graph diverges most and where the dedup bands actually live.
TEXTS = [
    "Deploy failed: migration not run\nThe deploy failed because the backfill "
    "migration was never executed before switching the mapper.",
    "Deploy failure, migration was not run\nThe deploy failed since the backfill "
    "migration was not executed prior to switching the mapper.",
    "Dad prefers morning appointments\nBook medical appointments before 11am.",
    "Coffee maker needs descaling monthly\nDescale the office machine every month.",
    "how do I descale the coffee machine",
]


def _torch_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except Exception:
        return False
    return True


def _cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


# --- Profile selection ------------------------------------------------------

def test_default_profile_is_a_known_profile():
    assert embedding.DEFAULT_PROFILE in embedding.PROFILES


def test_unset_env_selects_the_default(monkeypatch):
    monkeypatch.delenv("ENGRAPHY_EMBEDDING_PROFILE", raising=False)
    assert embedding.profile() == embedding.DEFAULT_PROFILE


def test_env_selects_the_named_profile(monkeypatch):
    for name in embedding.PROFILES:
        monkeypatch.setenv("ENGRAPHY_EMBEDDING_PROFILE", name)
        assert embedding.profile() == name


def test_unknown_profile_fails_loudly_rather_than_falling_back(monkeypatch):
    """Same posture as a malformed config value (design/07): an operator who
    typoed the profile must not get a store embedded by the wrong pipeline."""
    monkeypatch.setenv("ENGRAPHY_EMBEDDING_PROFILE", "onnx-int4")
    with pytest.raises(embedding.UnknownEmbeddingProfile) as exc:
        embedding.profile()
    assert "onnx-int4" in str(exc.value)
    for name in embedding.PROFILES:
        assert name in str(exc.value)


# --- The stamp --------------------------------------------------------------

def test_stamp_distinguishes_every_profile():
    """`MODEL_ID` is one string for all three backends, so the stamp is what lets
    `engraphy-admin reembed` tell an int8 row from a torch one. If any two
    profiles shared a stamp the backfill would skip rows it must rewrite."""
    stamps = {name: embedding.model_stamp(name) for name in embedding.PROFILES}
    assert len(set(stamps.values())) == len(embedding.PROFILES)
    for name, stamp in stamps.items():
        assert stamp.startswith(embedding.MODEL_ID)


def test_legacy_torch_stamp_is_the_bare_model_id():
    """Rows written before the seam existed carry the bare id. Keeping
    `legacy-torch` on that exact string means those rows are already correctly
    labelled and a backfill does not have to guess at their provenance."""
    assert embedding.model_stamp("legacy-torch") == embedding.MODEL_ID


def test_module_stamp_matches_the_active_profile():
    assert embedding.MODEL_STAMP == embedding.model_stamp(embedding.DEFAULT_PROFILE) or \
        embedding.MODEL_STAMP == embedding.model_stamp(
            os.environ.get("ENGRAPHY_EMBEDDING_PROFILE", embedding.DEFAULT_PROFILE))


# --- Shared invariants hold on every profile --------------------------------

@pytest.mark.parametrize("name", ["onnx-fp32", "onnx-int8"])
def test_onnx_profiles_are_384_dim_unit_vectors(name):
    vec = embedding.embed_with(name, TEXTS[0])
    assert len(vec) == embedding.DIMS == 384
    assert abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < 1e-6


@pytest.mark.parametrize("name", ["onnx-fp32", "onnx-int8"])
def test_onnx_profiles_are_deterministic(name):
    assert embedding.embed_with(name, TEXTS[0]) == embedding.embed_with(name, TEXTS[0])


def test_onnx_does_not_import_torch():
    """The ONNX profiles exist partly to take torch off the default path. If an
    import crept back the memory win would evaporate silently."""
    import subprocess
    import sys

    code = (
        "import sys;"
        "from engraphy.core import embedding;"
        "embedding.embed_with('onnx-int8', 'probe');"
        "print('torch' in sys.modules, 'transformers' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False", out.stdout


# --- Parity: the claim the promotion rests on -------------------------------

@pytest.mark.skipif(not _torch_available(), reason="legacy-torch backend not installed")
def test_onnx_fp32_reproduces_legacy_torch():
    """The de-risking claim, stated as a measurement: same weights, different
    executor, same vector to within float noise."""
    for text in TEXTS:
        torch_vec = embedding.embed_with("legacy-torch", text)
        onnx_vec = embedding.embed_with("onnx-fp32", text)
        cos = _cos(torch_vec, onnx_vec)
        worst = max(abs(a - b) for a, b in zip(torch_vec, onnx_vec))
        assert cos > 0.9999, f"fp32 parity broke on {text[:40]!r}: cos={cos}"
        assert worst < 1e-3, f"fp32 parity broke on {text[:40]!r}: max|diff|={worst}"


@pytest.mark.skipif(not _torch_available(), reason="legacy-torch backend not installed")
def test_onnx_fp32_preserves_pairwise_similarity():
    """Parity that matters for dedup is not per-vector closeness but whether the
    PAIRWISE cosines the bands read come out the same."""
    for i in range(len(TEXTS)):
        for j in range(i + 1, len(TEXTS)):
            t = _cos(embedding.embed_with("legacy-torch", TEXTS[i]),
                     embedding.embed_with("legacy-torch", TEXTS[j]))
            o = _cos(embedding.embed_with("onnx-fp32", TEXTS[i]),
                     embedding.embed_with("onnx-fp32", TEXTS[j]))
            assert abs(t - o) < 1e-3, f"pair ({i},{j}) moved {t:.6f} -> {o:.6f}"


@pytest.mark.skipif(not _torch_available(), reason="legacy-torch backend not installed")
def test_onnx_int8_diverges_but_stays_bounded_where_the_bands_read():
    """int8 is a DIFFERENT vector space and the suite says so out loud.

    The bound is deliberately scoped to the region the bands actually read.
    Quantization drift is not uniform: measured here, a near-identical pair moves
    by 0.012 while an unrelated mid-similarity pair moves by 0.041. Only the
    former is near a band edge. A pair sitting at cosine 0.5 can move twice as far
    without changing any decision, because 0.5 is `insert` before and after.

    So: pairs in the banded region (at or above `dedup.t_low`) must stay inside
    the drift the 0.94 recalibration was derived under. Across the 17 committed
    dedup fixtures that drift measured 0.0144 mean and 0.0293 max; the bound here
    carries headroom over the max rather than sitting on it.

    Compared document-to-document, with the document prefix on both sides,
    because that is exactly how dedup and resonance compare (design/02).
    """
    banded_worst = 0.0
    overall_worst = 0.0
    for i in range(len(TEXTS)):
        for j in range(i + 1, len(TEXTS)):
            a, b = embedding.DOCUMENT_PREFIX + TEXTS[i], embedding.DOCUMENT_PREFIX + TEXTS[j]
            t = _cos(embedding.embed_with("legacy-torch", a),
                     embedding.embed_with("legacy-torch", b))
            q = _cos(embedding.embed_with("onnx-int8", a),
                     embedding.embed_with("onnx-int8", b))
            overall_worst = max(overall_worst, abs(t - q))
            if t >= 0.80:                     # dedup.t_low: the banded region
                banded_worst = max(banded_worst, abs(t - q))

    assert overall_worst > 1e-4, "int8 matched fp32 exactly; the profiles are not distinct"
    assert banded_worst < 0.05, (
        f"int8 drift in the banded region is {banded_worst:.4f}, beyond what the "
        f"0.94 t_high recalibration assumes. Re-derive the band before shipping.")
