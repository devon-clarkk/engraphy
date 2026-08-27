"""engraphy.core.embedding — design/07 §Exact formulas: pinned model, 384 dims after
truncation, L2-re-normalized (unit vectors). No golden-value fixture (embeddings
aren't hand-computable) — these are the "norm test" 07's module order calls for.
"""
import math

from engraphy.core import embedding


def test_model_id_and_revision_are_pinned():
    assert embedding.MODEL_ID == "nomic-ai/nomic-embed-text-v1.5"
    assert embedding.MODEL_REVISION  # non-empty — never floats on a branch head
    assert len(embedding.MODEL_REVISION) == 40  # full git commit SHA, not a short hash


def test_embed_returns_384_dims():
    vec = embedding.embed("a\nb")
    assert len(vec) == embedding.DIMS == 384


def test_embed_returns_unit_norm():
    vec = embedding.embed("Deploy failed\nThe migration was never run before the switch.")
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-6


def test_embed_is_deterministic():
    text = "Same title\nSame body, called twice."
    assert embedding.embed(text) == embedding.embed(text)


def test_embed_distinguishes_different_text():
    a = embedding.embed("Coffee maker needs descaling\nDescale monthly or it breaks.")
    b = embedding.embed("Recipe for pancakes\nMix flour eggs and milk.")
    assert a != b
    dot = sum(x * y for x, y in zip(a, b))
    assert dot < 0.9  # unrelated text should not be near-identical after truncation+renorm


# Task prefixes (QUESTIONS.md embedding-task-prefix, Fable option b): the wrappers
# prepend the prefix the ACTIVE profile's model expects and defer to core embed().
# Read through document_prefix() / query_prefix() rather than the module constants,
# which are nomic's: a profile running a model that was trained without a task
# instruction takes none (core/embedding.py, section Backends).

def test_embed_document_prepends_document_prefix():
    text = "A title\nA body."
    assert embedding.embed_document(text) == embedding.embed(
        embedding.document_prefix() + text)


def test_embed_query_prepends_query_prefix():
    text = "how do I descale the coffee machine"
    assert embedding.embed_query(text) == embedding.embed(embedding.query_prefix() + text)


def test_the_legs_agree_with_the_model_about_asymmetry():
    """Whether the two legs differ is the MODEL's design, not this engine's, so
    the assertion follows the profile's own prefixes. nomic was trained with two
    distinct task instructions and its legs must differ; a model trained without
    one is symmetric on both legs. Demanding one shape for every profile would be
    demanding that a model do something it was never trained to do."""
    text = "Coffee maker needs descaling\nDescale monthly."
    doc, query = embedding.embed_document(text), embedding.embed_query(text)
    if embedding.document_prefix() == embedding.query_prefix():
        assert doc == query
    else:
        assert doc != query


def test_embed_document_is_unit_norm():
    vec = embedding.embed_document("Deploy failed\nThe migration was never run.")
    assert len(vec) == embedding.DIMS
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-6
