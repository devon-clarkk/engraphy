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
# prepend the pinned model's mandated prefix and defer to core embed().

def test_embed_document_prepends_document_prefix():
    text = "A title\nA body."
    assert embedding.embed_document(text) == embedding.embed(embedding.DOCUMENT_PREFIX + text)


def test_embed_query_prepends_query_prefix():
    text = "how do I descale the coffee machine"
    assert embedding.embed_query(text) == embedding.embed(embedding.QUERY_PREFIX + text)


def test_document_and_query_prefixes_differ():
    text = "Coffee maker needs descaling\nDescale monthly."
    assert embedding.embed_document(text) != embedding.embed_query(text)


def test_embed_document_is_unit_norm():
    vec = embedding.embed_document("Deploy failed\nThe migration was never run.")
    assert len(vec) == embedding.DIMS
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-6
