"""Embedding pipeline. Normative: design/02 (revised decision) + design/07 §Exact formulas.

Default model: nomic-embed-text-v1.5, truncated to first 384 dims, then L2-RE-NORMALIZED
(unit vectors => similarity == dot == 1 - cosine_distance). Fallback: bge-small-en-v1.5.
Loaded ONCE at process start. embed() is pure and MUST NOT be called inside a DB
transaction (design/implementation/dedup-write-path-plan.md, trap 3 — CI grep enforces).

Task prefixes (QUESTIONS.md "embedding-task-prefix", resolved 2026-07-16, Fable
option b): the pinned model's card mandates a task-instruction prefix, so callers
use embed_document()/embed_query() rather than bare embed(). Stored and compared
node text (write, dedup candidates, resonance, update re-embeds, import) is
"search_document: " + title + "\n" + body; a search query leg is
"search_query: " + query. Prefix concatenated directly, no extra separator, as
the card shows. Dedup and resonance compare document-vs-document, so banding stays
symmetric; only search is asymmetric, which is the model's own retrieval design.
Core embed() and its norm invariants are unchanged — it is the shared primitive
the two wrappers call.
"""
import math
import os
import pathlib

MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
# Pinned commit, not "main" -- trust_remote_code=True executes the repo's own model code,
# so floating on the branch head would mean an upstream push runs arbitrary code here.
MODEL_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"
DIMS = 384

_model = None


class ModelCacheNotWritable(RuntimeError):
    """The HuggingFace cache directory exists but this process cannot write to
    it, so the model can neither be downloaded nor cached. Raised in place of a
    bare PermissionError from deep inside huggingface_hub, whose traceback names
    an internal blob path and gives the operator nothing actionable."""


def _cache_dir() -> pathlib.Path:
    """Where huggingface_hub will try to write. HF_HOME is what the container
    sets; the library's own default is ~/.cache/huggingface."""
    hf_home = os.environ.get("HF_HOME")
    return pathlib.Path(hf_home) if hf_home else pathlib.Path.home() / ".cache" / "huggingface"


def load_model() -> None:
    global _model
    if _model is not None:
        return
    from sentence_transformers import SentenceTransformer

    try:
        _model = SentenceTransformer(MODEL_ID, revision=MODEL_REVISION, trust_remote_code=True)
    except PermissionError as exc:
        # The cloud profile mounts a named volume at HF_HOME. If the image did
        # not create that directory first, Docker creates the mountpoint
        # root-owned and this process (uid 1000) cannot write it -- the server
        # then crash-loops before serving anything, and the raw PermissionError
        # points at a huggingface_hub blob path that explains none of it. This
        # cost ~10 minutes of misdiagnosis during the first deploy walkthrough
        # (it reads exactly like a slow model download), so name the cause and
        # the fix instead.
        cache = _cache_dir()
        raise ModelCacheNotWritable(
            f"cannot write the embedding-model cache at {cache} "
            f"(uid={os.getuid() if hasattr(os, 'getuid') else 'n/a'}): {exc}\n"
            f"The model (~523MB) must be downloaded there on first boot.\n"
            f"If this is the Docker/compose profile, the model-cache volume is "
            f"probably root-owned because the image did not create HF_HOME "
            f"before the volume was mounted over it. Fix the image (mkdir -p + "
            f"chown to the runtime user before USER), or repair the existing "
            f"volume once with:\n"
            f"  docker run --rm -v <project>_model-cache:/cache alpine "
            f"chown -R 1000:1000 /cache\n"
            f"Otherwise ensure {cache} is writable by the user running engraphy."
        ) from exc


# The pinned model's task-instruction prefixes (nomic-embed-text-v1.5 card).
# Colon + single space, concatenated directly onto the text -- no extra separator.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


def render_attr_surface(attrs: dict, searchable_keys: set) -> str:
    """Phase C (fact-searchability-phase-c.md §2.1): deterministic render of a
    node's searchable attrs into the extra searchable text. The searchable keys
    (resolved from the type's attr_spec by the §1 rule) that are PRESENT in
    `attrs`, in lexicographic order, one `key: value` line each; values as stored
    (dates ISO, strings verbatim, no truncation); None and empty-string values
    skipped. Returns '' when nothing renders -- an attr-less or all-excluded node
    renders '' and its searchable_text is byte-identical to title+"\\n"+body,
    which §3's bounding argument relies on.

    Pure and deterministic (fixture-pinned); the single place attr text is shaped
    for the surface, so the embedding and the tsvector's weight-C leg read one
    render (the write path stores it in nodes.extra_search)."""
    lines = []
    for key in sorted(searchable_keys):
        if key not in attrs:
            continue
        value = attrs[key]
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def searchable_text(title: str, body: str, extra: str) -> str:
    """THE embedded document (fact-searchability-phase-c.md §2.1): title + "\\n" +
    body, plus "\\n" + extra when extra is non-empty. Every embed_document call
    site outside this module passes this function's output (CI-grepped by
    scripts/check_searchable_text_single_source.py). `extra == ""` reproduces the
    pre-Phase-C document exactly."""
    return title + "\n" + body + ("\n" + extra if extra else "")


def embed(text: str) -> list[float]:
    """384-dim unit-norm vector. The shared primitive; prefer embed_document /
    embed_query, which prepend the pinned model's mandated task prefix."""
    if _model is None:
        load_model()
    raw = _model.encode(text, normalize_embeddings=False)
    truncated = raw[:DIMS].tolist()
    norm = math.sqrt(sum(x * x for x in truncated))
    if norm == 0:
        return truncated
    return [x / norm for x in truncated]


def embed_document(text: str) -> list[float]:
    """Embed stored/compared node text (write path, dedup candidates, resonance,
    update re-embeds, import). Prepends "search_document: "; the caller passes the
    already-joined title + "\\n" + body."""
    return embed(DOCUMENT_PREFIX + text)


def embed_query(text: str) -> list[float]:
    """Embed a search query leg. Asymmetric with embed_document by design -- that
    asymmetry is the model's own retrieval training (design/02 §Search)."""
    return embed(QUERY_PREFIX + text)
