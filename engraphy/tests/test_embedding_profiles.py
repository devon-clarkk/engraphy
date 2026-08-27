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
    ("Deploy failed: migration not run\nThe deploy failed because the backfill "
     "migration was never executed before switching the mapper."),
    ("Deploy failure, migration was not run\nThe deploy failed since the backfill "
     "migration was not executed prior to switching the mapper."),
    "Dad prefers morning appointments\nBook medical appointments before 11am.",
    "Coffee maker needs descaling monthly\nDescale the office machine every month.",
    "how do I descale the coffee machine",
]


def _torch_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
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

def test_stamp_names_the_vector_space_not_the_executor():
    """The stamp exists so `engraphy-admin reembed` can tell which rows sit in
    which vector space. Profiles that produce interchangeable vectors must share
    it, or the backfill would rewrite rows that are already correct; profiles
    that do not must differ, or it would skip rows it has to rewrite."""
    assert embedding.model_stamp("legacy-torch") == embedding.model_stamp("onnx-fp32")
    assert embedding.model_stamp("onnx-int8") != embedding.model_stamp("onnx-fp32")
    assert embedding.model_stamp("micro") != embedding.model_stamp("onnx-int8")
    # The stamp leads with the MODEL, read from the profile's own spec rather
    # than the module constant. `micro` runs a different model entirely, so a
    # store part-way through a conversion onto it is legible at a glance and
    # `reembed` selects exactly the rows still in the old space.
    for name in embedding.PROFILES:
        assert embedding.model_stamp(name).startswith(embedding.spec(name).model_id)


def test_every_profile_stamps_a_distinct_vector_space():
    """One stamp per SPACE, and every space distinct. Two profiles sharing a
    stamp must produce interchangeable vectors (asserted for the fp32 pair
    below); anything else would make `reembed` skip rows it has to rewrite."""
    stamps = {name: embedding.model_stamp(name) for name in embedding.PROFILES}
    shared = [n for n in embedding.PROFILES if stamps[n] == embedding.MODEL_ID]
    assert sorted(shared) == sorted(embedding._FP32_EQUIVALENT)
    distinct = {stamps[n] for n in embedding.PROFILES}
    # The fp32-equivalent profiles collapse to one stamp; every other profile
    # contributes its own.
    assert len(distinct) == len(embedding.PROFILES) - len(embedding._FP32_EQUIVALENT) + 1


def test_fp32_profiles_carry_the_bare_model_id():
    """Rows written before the seam existed carry the bare id. Both fp32-equivalent
    profiles keep that exact string, so those rows stay correctly labelled and the
    torch-to-ONNX flip is a restart with no data implication at all."""
    assert embedding.model_stamp("legacy-torch") == embedding.MODEL_ID
    assert embedding.model_stamp("onnx-fp32") == embedding.MODEL_ID


def test_module_stamp_matches_the_active_profile():
    assert embedding.model_stamp(embedding.DEFAULT_PROFILE) == embedding.MODEL_STAMP or \
        embedding.model_stamp(
            os.environ.get("ENGRAPHY_EMBEDDING_PROFILE", embedding.DEFAULT_PROFILE)) == embedding.MODEL_STAMP


# --- Shared invariants hold on every profile --------------------------------

#: Every profile that runs a serialized graph. `micro` is in here for the same
#: reason the others are: whatever model it runs, it owes the engine a 384-dim
#: unit vector, and that is what makes the seam a seam rather than a fork.
_ONNX_PROFILES = ["onnx-fp32", "onnx-int8", "micro"]


@pytest.mark.parametrize("name", _ONNX_PROFILES)
def test_onnx_profiles_are_384_dim_unit_vectors(name):
    vec = embedding.embed_with(name, TEXTS[0])
    assert len(vec) == embedding.DIMS == 384
    assert abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < 1e-6


@pytest.mark.parametrize("name", _ONNX_PROFILES)
def test_onnx_profiles_are_deterministic(name):
    assert embedding.embed_with(name, TEXTS[0]) == embedding.embed_with(name, TEXTS[0])


def test_micro_is_a_genuinely_different_vector_space():
    """`micro` is the only profile that changes the MODEL, so the property to
    assert is not bounded drift (as it is for int8, below) but the opposite: the
    pairwise similarities it reads must NOT be a small perturbation of nomic's,
    or the whole re-embed requirement would be theatre.

    Stated as pairwise cosines rather than per-vector distance, because pairwise
    is what the dedup bands actually read, and because two unrelated models can
    agree closely on an easy pair while disagreeing on the ones near an edge."""
    moved = []
    for i in range(len(TEXTS)):
        for j in range(i + 1, len(TEXTS)):
            base = _cos(embedding.embed_with("onnx-int8",
                                             embedding.DOCUMENT_PREFIX + TEXTS[i]),
                        embedding.embed_with("onnx-int8",
                                             embedding.DOCUMENT_PREFIX + TEXTS[j]))
            micro = _cos(embedding.embed_with("micro", TEXTS[i]),
                         embedding.embed_with("micro", TEXTS[j]))
            moved.append(abs(base - micro))
    assert max(moved) > 0.05, (
        "gte-small reads these pairs the same way nomic does, which the "
        "calibration and the mandatory re-embed both assume it does not")


def test_micro_takes_no_task_prefix():
    """gte-small was trained without a task instruction. Prepending nomic's would
    be embedding a string the model has never seen, and the band calibration was
    derived without it, so this is a correctness pin rather than a style one."""
    assert embedding.document_prefix("micro") == ""
    assert embedding.query_prefix("micro") == ""
    assert embedding.document_prefix("onnx-int8") == embedding.DOCUMENT_PREFIX
    assert embedding.query_prefix("onnx-int8") == embedding.QUERY_PREFIX


def test_micro_reads_a_whole_node_not_its_opening():
    """gte-small's published tokenizer truncates at 128 tokens, which would embed
    a long node from its first paragraph and surface nothing. The seam sets the
    cap explicitly at the model's own 512-token limit, so this asserts that a
    node past 128 tokens still moves the vector.

    Two long texts sharing an opening and diverging only after the old cutoff:
    if the cap were still 128 they would embed identically."""
    # ~15 tokens per repeat, so 16 repeats clears 128 by a wide margin and
    # stays well inside the 512 cap the seam sets.
    opening = "The quarterly deployment review covered the migration schedule. " * 16
    a = embedding.embed_with("micro", opening + " The rollback was executed on Tuesday.")
    b = embedding.embed_with("micro", opening + " The database was restored from backup.")
    assert len(embedding._backend_for("micro")._tok.encode(opening).ids) > 128
    assert _cos(a, b) < 0.999, (
        "two nodes that differ only past token 128 embed identically, so the "
        "tokenizer is still truncating at the model repo's published default")


@pytest.mark.parametrize("name", ["onnx-int8", "micro"])
def test_onnx_does_not_import_torch(name):
    """The ONNX profiles exist partly to take torch off the default path. If an
    import crept back the memory win would evaporate silently, and on `micro` it
    would evaporate a claim four times its own size: torch alone puts a 462MB
    floor under a process whose whole point is a 143MB one."""
    import subprocess
    import sys

    code = (
        "import sys;"
        "from engraphy.core import embedding;"
        f"embedding.embed_with({name!r}, 'probe');"
        "print('torch' in sys.modules, 'transformers' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=False)
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
