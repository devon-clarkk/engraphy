"""Embedding pipeline. Normative: design/02 (revised decision) + design/07 §Exact formulas.

Model: nomic-embed-text-v1.5, truncated to first 384 dims, then L2-RE-NORMALIZED
(unit vectors => similarity == dot == 1 - cosine_distance). Loaded ONCE at process
start. embed() is pure and MUST NOT be called inside a DB transaction
(design/implementation/dedup-write-path-plan.md, trap 3 -- CI grep enforces).

Task prefixes (QUESTIONS.md "embedding-task-prefix", resolved 2026-07-16, Fable
option b): the pinned model's card mandates a task-instruction prefix, so callers
use embed_document()/embed_query() rather than bare embed(). Stored and compared
node text (write, dedup candidates, resonance, update re-embeds, import) is
"search_document: " + title + "\n" + body; a search query leg is
"search_query: " + query. Prefix concatenated directly, no extra separator, as
the card shows. Dedup and resonance compare document-vs-document, so banding stays
symmetric; only search is asymmetric, which is the model's own retrieval design.
Core embed() and its norm invariants are unchanged: it is the shared primitive
the two wrappers call.

## Backends

`ENGRAPHY_EMBEDDING_PROFILE` selects. Every profile produces a 384-dim unit
vector through the same truncate-and-renormalize tail, so the seam is the only
thing that varies and no profile needs a schema change.

Three of them run ONE model at ONE pinned revision, nomic-embed-text-v1.5:

  `onnx-int8`     ONNX Runtime over `onnx/model_quantized.onnx`.
  `onnx-fp32`     ONNX Runtime over `onnx/model.onnx`. Reproduces the torch
                  vectors to within float noise, so a store written by either is
                  directly comparable and moving between them needs no re-embed.
  `legacy-torch`  sentence-transformers over `model.safetensors`.

The fourth runs a different model, and it is the one thing in this module that
is not the same weights through a different executor:

  `micro`         ONNX Runtime over gte-small's vendor-published int8 graph,
                  for hosts where the memory matters more than the last few
                  points of recall. It is a different vector space, so adopting
                  it on an existing store is a re-embed, not a restart. See
                  `_Spec` below for what varies, and `docs/micro-reembed.md`
                  for the adoption runbook.

The ONNX profiles execute a serialized graph and no repository Python, so
`trust_remote_code` is not used on those paths and torch is never imported. Every
revision stays pinned: one repo serves every artifact of a given model, and a
floating revision would change the weights under a calibrated dedup band.

Vectors from `onnx-int8` are NOT interchangeable with the other two. Quantization
contracts pairwise cosine, which moves near-identical pairs across `dedup.t_high`.
That is why the int8 profile ships its own band defaults and its own fixture set,
and why switching an existing store onto it wants `engraphy-admin reembed` rather
than a bare restart. `MODEL_STAMP` records which pipeline produced a row so that
backfill is resumable and idempotent.

`onnx-int8` is opt-in, and the reason is a measurement rather than caution.
Quantized arithmetic varies with the host CPU, and around `dedup.t_low` that
variation is larger than the margin int8 leaves. Two committed fixtures sit either
side of the confirm edge; across two Linux x86-64 hosts running identical code
they measured 0.8072 / 0.8197 on one and 0.7809 / 0.8017 on the other, so the
viable `t_low` windows were (0.8072, 0.8197] and (0.7809, 0.8017]. Those do not
intersect: no single shipped default reproduces the same banding on both. The fp32
space leaves 0.040 of room at the same edge and is bit-reproducible, which is why
it holds the default.

Running int8 is supported and worthwhile where the memory matters. Calibrate
`dedup.t_low` against the target hardware first, with
`scripts/baseline_dedup_fixtures_profile.py`, and set it per space in `config`.

## Choosing `micro`

`micro` trades retrieval accuracy for resident memory, and both sides of that
trade are measured rather than estimated. Against `onnx-int8` on the same probe
and the same corpus: 143MB steady against 277MB, and recall@10 0.6518 against
0.6923. Its band defaults are derived from scratch in `core/dedup.py`, because a
different model's similarities carry over from nothing.

Adopting it is a full re-embed, always. gte-small's vectors are not comparable
with nomic's on any axis a cosine reads, so an incoming node embedded by `micro`
banded against stored nomic vectors is comparing two different spaces.
`MODEL_STAMP` names the model as well as the profile, so `engraphy-admin reembed`
selects exactly the rows still in the old space and can resume mid-run.
"""
import dataclasses
import functools
import math
import os
import pathlib

MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
# Pinned commit, not "main". The legacy-torch profile runs the repo's own model
# code under trust_remote_code=True, so floating on the branch head would mean an
# upstream push executes arbitrary code here. The ONNX profiles do not execute
# repository Python, but they stay pinned anyway: the dedup bands are calibrated
# against these exact weights, so an upstream reupload would silently move them.
MODEL_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"

# The `micro` profile's model, and a VENDOR-published int8 graph rather than one
# quantized here. Pinned for the same reason the nomic revision is: the dedup
# bands are calibrated against these exact weights.
MICRO_MODEL_ID = "Xenova/gte-small"
MICRO_MODEL_REVISION = "5927d1727bb12db490052a1b33265ad78058de08"

DIMS = 384

PROFILES = ("onnx-int8", "onnx-fp32", "legacy-torch", "micro")
DEFAULT_PROFILE = "onnx-fp32"
_PROFILE_ENV = "ENGRAPHY_EMBEDDING_PROFILE"


@dataclasses.dataclass(frozen=True)
class _Spec:
    """Everything that varies between profiles, in one place.

    The seam exists so that adding a backend is a row here plus a calibration,
    never a branch scattered through the module. `graph=None` selects the torch
    backend; every other field is read by exactly one caller.
    """

    model_id: str
    revision: str
    graph: str | None
    doc_prefix: str
    query_prefix: str
    #: Cap handed to the tokenizer, or None to leave the model repo's own
    #: tokenizer configuration standing. See `_OnnxBackend` for why micro sets one.
    max_tokens: int | None


#: The nomic task-instruction prefixes (nomic-embed-text-v1.5 card). Colon +
#: single space, concatenated directly onto the text, no extra separator.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

_SPECS = {
    "onnx-int8": _Spec(MODEL_ID, MODEL_REVISION, "onnx/model_quantized.onnx",
                       DOCUMENT_PREFIX, QUERY_PREFIX, None),
    "onnx-fp32": _Spec(MODEL_ID, MODEL_REVISION, "onnx/model.onnx",
                       DOCUMENT_PREFIX, QUERY_PREFIX, None),
    "legacy-torch": _Spec(MODEL_ID, MODEL_REVISION, None,
                          DOCUMENT_PREFIX, QUERY_PREFIX, None),
    # gte-small takes NO task prefix: it was trained without one, and prepending
    # nomic's would be measuring our own bug rather than the model. The 512 cap
    # is a correctness requirement, not a tuning choice -- see `_OnnxBackend`.
    "micro": _Spec(MICRO_MODEL_ID, MICRO_MODEL_REVISION, "onnx/model_quantized.onnx",
                   "", "", 512),
}


def spec(name: str | None = None) -> _Spec:
    """The active profile's model, graph, prefixes and token cap."""
    return _SPECS[name or profile()]


def document_prefix(name: str | None = None) -> str:
    """The task-instruction prefix this profile's model expects on stored and
    compared text. Empty for a model trained without one."""
    return spec(name).doc_prefix


def query_prefix(name: str | None = None) -> str:
    """The prefix this profile's model expects on a search query leg."""
    return spec(name).query_prefix

#: Profiles whose vectors are interchangeable, and therefore share a stamp.
#: `onnx-fp32` reproduces `legacy-torch` to float noise (asserted in
#: test_embedding_profiles.py), so a row embedded by either is the same row as
#: far as any cosine in this system is concerned. `onnx-int8` is a genuinely
#: different space and stamps separately.
_FP32_EQUIVALENT = ("legacy-torch", "onnx-fp32")

_model = None


class UnknownEmbeddingProfile(RuntimeError):
    """`ENGRAPHY_EMBEDDING_PROFILE` names a profile this build does not have.
    Raised instead of silently falling back, for the same reason a malformed
    config value fails a write loudly (design/07): an operator who typoed the
    profile would otherwise get a store embedded by the wrong pipeline and no
    signal until the dedup bands started behaving oddly."""


class ModelCacheNotWritable(RuntimeError):
    """The HuggingFace cache directory exists but this process cannot write to
    it, so the model can neither be downloaded nor cached. Raised in place of a
    bare PermissionError from deep inside huggingface_hub, whose traceback names
    an internal blob path and gives the operator nothing actionable."""


def profile() -> str:
    """The active backend. Unset means DEFAULT_PROFILE; an unknown value raises."""
    name = os.environ.get(_PROFILE_ENV) or DEFAULT_PROFILE
    if name not in PROFILES:
        raise UnknownEmbeddingProfile(
            f"{_PROFILE_ENV}={name!r} is not a known embedding profile. "
            f"Known: {', '.join(PROFILES)}."
        )
    return name


def model_stamp(name: str | None = None) -> str:
    """What goes in `nodes.embedding_model`.

    It names the VECTOR SPACE, not the executor, which is the distinction that
    makes the backfill correct. `legacy-torch` and `onnx-fp32` produce
    interchangeable vectors and therefore share the bare `MODEL_ID`: moving a
    store between those two profiles is a restart and nothing else, and
    `engraphy-admin reembed` correctly finds no work to do. `onnx-int8` is a
    different space and stamps separately, so the same command finds every row
    that still needs rewriting and can resume mid-run.

    The model id comes from the profile's own spec rather than the module
    constant, because `micro` runs a different model entirely. Its stamp names
    that model, so a store half-converted onto micro is legible at a glance and
    `reembed` selects exactly the rows still in the old space.
    """
    name = name or profile()
    if name in _FP32_EQUIVALENT:
        return MODEL_ID
    return f"{_SPECS[name].model_id}+{name}"


#: Stamped into `nodes.embedding_model` on every write and re-embed. Resolved once
#: at import: the profile is process-level configuration and a mid-process change
#: would mean one process writing rows in two vector spaces.
MODEL_STAMP = model_stamp()


def _cache_dir() -> pathlib.Path:
    """Where huggingface_hub will try to write. HF_HOME is what the container
    sets; the library's own default is ~/.cache/huggingface."""
    hf_home = os.environ.get("HF_HOME")
    return pathlib.Path(hf_home) if hf_home else pathlib.Path.home() / ".cache" / "huggingface"


def _cache_not_writable(exc: PermissionError) -> ModelCacheNotWritable:
    """The cloud profile mounts a named volume at HF_HOME. If the image did not
    create that directory first, Docker creates the mountpoint root-owned and
    this process (uid 1000) cannot write it: the server then crash-loops before
    serving anything, and the raw PermissionError points at a huggingface_hub
    blob path that explains none of it. This cost ~10 minutes of misdiagnosis
    during the first deploy walkthrough (it reads exactly like a slow model
    download), so name the cause and the fix instead."""
    cache = _cache_dir()
    return ModelCacheNotWritable(
        f"cannot write the embedding-model cache at {cache} "
        f"(uid={os.getuid() if hasattr(os, 'getuid') else 'n/a'}): {exc}\n"
        f"The model must be downloaded there on first boot.\n"
        f"If this is the Docker/compose profile, the model-cache volume is "
        f"probably root-owned because the image did not create HF_HOME "
        f"before the volume was mounted over it. Fix the image (mkdir -p + "
        f"chown to the runtime user before USER), or repair the existing "
        f"volume once with:\n"
        f"  docker run --rm -v <project>_model-cache:/cache alpine "
        f"chown -R 1000:1000 /cache\n"
        f"Otherwise ensure {cache} is writable by the user running engraphy."
    )


class _TorchBackend:
    """sentence-transformers over the safetensors weights."""

    def __init__(self, spec: _Spec):
        from sentence_transformers import SentenceTransformer

        try:
            self._st = SentenceTransformer(
                spec.model_id, revision=spec.revision, trust_remote_code=True)
        except PermissionError as exc:
            raise _cache_not_writable(exc) from exc

    def encode(self, text: str):
        return self._st.encode(text, normalize_embeddings=False)


class _OnnxBackend:
    """ONNX Runtime over one of the repo's exported graphs.

    Reproduces what sentence-transformers does for this model, which its
    `modules.json` states exactly: a Transformer followed by mean Pooling, and no
    normalization module (the tail in `embed()` supplies that). Deliberately raw
    `onnxruntime` + `tokenizers` rather than a wrapper library: the wrapper that
    was convenient for prototyping does not guarantee a revision pin, and pinning
    is the whole reason MODEL_REVISION exists.
    """

    def __init__(self, spec: _Spec):
        import numpy as np
        import onnxruntime
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        self._np = np
        try:
            model_path = hf_hub_download(spec.model_id, spec.graph, revision=spec.revision)
            tok_path = hf_hub_download(spec.model_id, "tokenizer.json", revision=spec.revision)
        except PermissionError as exc:
            raise _cache_not_writable(exc) from exc

        opts = onnxruntime.SessionOptions()
        # One intra-op thread, and this is measured rather than assumed. In
        # isolation more threads do win: a single query embed on a 4-core box is
        # 41.8ms at one thread against 21.1ms at the runtime's default. In the
        # serving path the opposite holds, because the embed does not run alone.
        # It runs beside an async connection pool and a local Postgres, and the
        # extra threads contend with both. Measured end to end by the benchmark
        # at 10k nodes, search p50:
        #
        #     1 thread    76.8ms
        #     2 threads  167.0ms
        #     unpinned   156.1ms
        #
        # The isolated number is the misleading one. Raise this only on a host
        # where the embedder has cores to itself.
        opts.intra_op_num_threads = int(os.environ.get("ENGRAPHY_EMBEDDING_THREADS", "1"))
        self._sess = onnxruntime.InferenceSession(
            model_path, opts, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._sess.get_inputs()}
        self._tok = Tokenizer.from_file(tok_path)
        # A model repo ships whatever tokenizer settings suited its own
        # distribution, and those are not always what a memory engine wants.
        # gte-small's arrives configured to truncate at 128 tokens and pad every
        # sequence to exactly 128, which is a sensible default for the
        # browser-side port it was published for and silently wrong here: a node
        # longer than about a hundred words would be embedded from its opening
        # only, and nothing would surface that. Setting the cap explicitly makes
        # the window a property of THIS pipeline rather than of an upstream
        # file. 512 is the model's own `max_position_embeddings`; a BERT with
        # absolute position embeddings cannot be handed more.
        #
        # The nomic profiles leave this None. Their tokenizer sets no truncation
        # and the model takes 8192 tokens, so a cap here would only remove text
        # the default path keeps today.
        if spec.max_tokens is not None:
            self._tok.enable_truncation(max_length=spec.max_tokens)
            self._tok.no_padding()

    def encode(self, text: str):
        np = self._np
        enc = self._tok.encode(text)
        ids = np.asarray([enc.ids], dtype=np.int64)
        mask = np.asarray([enc.attention_mask], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self._sess.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0]
        # Mean pooling over the attended tokens, matching 1_Pooling's config.
        m = mask[..., None].astype(np.float32)
        return ((hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None))[0]


def _build(name: str):
    s = _SPECS[name]
    return _TorchBackend(s) if s.graph is None else _OnnxBackend(s)


def load_model() -> None:
    """Load the active profile's backend AND put it in its serving state.
    Idempotent; called once at process start.

    The warm-up embed is not decoration. An ONNX inference session defers real
    work to its first run: it allocates the memory arenas and finishes preparing
    the graph. Constructing the session is therefore not the same as being ready,
    and without this the first request a process serves pays that cost. The
    design says the model is loaded at boot, so a served query should never see
    it."""
    global _model
    if _model is not None:
        return
    name = profile()
    model = _build(name)
    model.encode(document_prefix(name) + "warm the inference session")
    _model = model


@functools.cache
def _backend_for(name: str):
    """A named backend, independent of the process-wide one. Exists for the
    parity test, which must hold two backends at once to compare them; the
    serving path always goes through load_model()."""
    return _build(name)


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


def _truncate_and_normalize(raw) -> list[float]:
    """The tail every profile shares: first DIMS dims (nomic v1.5 is Matryoshka,
    so a prefix is a valid embedding), then L2 to a unit vector."""
    truncated = [float(x) for x in raw[:DIMS]]
    norm = math.sqrt(sum(x * x for x in truncated))
    if norm == 0:
        return truncated
    return [x / norm for x in truncated]


def embed(text: str) -> list[float]:
    """384-dim unit-norm vector. The shared primitive; prefer embed_document /
    embed_query, which prepend the pinned model's mandated task prefix.

    One text per call, deliberately. The int8 graph is not batch-invariant (a
    batch pads to its longest member and quantized activations see a different
    pad length), so a batched encode returns vectors that differ from what this
    path produces for the same text. Anything re-embedding stored rows must go
    through here, one row at a time, or it writes vectors the write path would
    not have written."""
    if _model is None:
        load_model()
    return _truncate_and_normalize(_model.encode(text))


def embed_with(profile_name: str, text: str) -> list[float]:
    """`embed` against a named profile rather than the process-wide one. For the
    parity test and for offline comparison only; serving uses `embed`."""
    return _truncate_and_normalize(_backend_for(profile_name).encode(text))


def embed_document(text: str) -> list[float]:
    """Embed stored/compared node text (write path, dedup candidates, resonance,
    update re-embeds, import). Prepends the active profile's document prefix
    ("search_document: " on the nomic profiles, empty on `micro`, whose model was
    trained without one); the caller passes the
    already-joined title + "\\n" + body."""
    return embed(document_prefix() + text)


def embed_query(text: str) -> list[float]:
    """Embed a search query leg. Asymmetric with embed_document by design -- that
    asymmetry is the model's own retrieval training (design/02 §Search). A
    profile whose model takes no prefix is symmetric on both legs, which is that
    model's own design rather than a departure from this one."""
    return embed(query_prefix() + text)
