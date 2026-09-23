"""The scorer seam for the Jev rerank experiment.

A scorer maps (query, candidates) to a relevance score in [0, 1] per candidate
id. Two implementations:

- `JevClient` -- TypeSafe's Jev over HTTP. **The wire format is a stub.** It was
  written before an account existed and without access to TypeSafe's API
  reference, so `_request_body` and `_parse_score` are placeholders to replace
  with the documented shapes. Everything else (auth from the environment,
  concurrency, the on-disk cache, cost accounting) is real.
- `LexicalScorer` -- token-overlap between query and memory. No network, no
  key. It is the control arm: if Jev cannot beat a free heuristic over the same
  candidates, it is not earning its call.

Configuration is environment-only, so a key never lands in a command line or a
committed file:

    JEV_API_KEY        required for --scorer jev
    JEV_API_URL        required for --scorer jev (no default: the endpoint is
                       whatever the docs say, not a guess baked in here)
    JEV_MODEL          optional, sent through if the API takes a model field
    JEV_CONCURRENCY    optional, parallel requests (default 8)
    JEV_TIMEOUT_S      optional, per-request timeout (default 30)
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

# The one question every candidate is asked. Versioned into the cache key, so
# editing the wording never serves scores produced by the old wording.
RELEVANCE_QUESTION = (
    "How useful is this stored memory for answering the query? "
    "1.0 means it directly contains the answer, 0.0 means it is unrelated."
)

# Published list price at the time of writing: $0.042 per million input
# tokens, output unmetered. Used only for the pre-run estimate.
USD_PER_M_INPUT_TOKENS = 0.042


class Scorer(Protocol):
    name: str

    def score(self, query: str, candidates: list[tuple[str, str]]) -> dict[str, float]:
        """candidates: [(node_id, text)]. Returns node_id -> score in [0, 1]."""
        ...


class JevConfigError(RuntimeError):
    pass


class JevResponseError(RuntimeError):
    pass


def estimate_tokens(text: str) -> int:
    """~4 characters per token. An estimate for the cost preview, not billing."""
    return max(1, len(text) // 4)


# ---- offline control arm ------------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


@dataclass
class LexicalScorer:
    """Fraction of the query's distinct tokens that appear in the memory."""

    name: str = "lexical"

    def score(self, query: str, candidates: list[tuple[str, str]]) -> dict[str, float]:
        q = _tokens(query)
        if not q:
            return {nid: 0.0 for nid, _ in candidates}
        return {nid: len(q & _tokens(text)) / len(q) for nid, text in candidates}


# ---- Jev ----------------------------------------------------------------------


def _request_body(query: str, memory: str, model: str | None) -> dict:
    """STUB: the request JSON for one (query, memory) relevance question.

    Shape guessed from public write-ups (a block of state plus typed questions,
    a `score` question returning a fraction). Replace with the documented
    schema once you have API access."""
    body = {
        "state": {"query": query, "memory": memory},
        "questions": [{"id": "relevance", "type": "score", "question": RELEVANCE_QUESTION}],
    }
    if model:
        body["model"] = model
    return body


def _parse_score(payload: dict) -> float:
    """STUB: pull the relevance score out of one response.

    Expects `{"answers": [{"id": "relevance", "value": <float>}]}`. Replace with
    the documented shape. Fails loudly rather than defaulting, so a wrong guess
    here shows up as an error on the first call, not as silently-flat scores
    that look like "Jev did not help"."""
    try:
        answers = {a["id"]: a for a in payload["answers"]}
        value = float(answers["relevance"]["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JevResponseError(
            f"unexpected Jev response shape ({exc!r}); update _parse_score in "
            f"bench/jev/client.py to the documented schema. Response was: "
            f"{json.dumps(payload)[:500]}") from exc
    if not 0.0 <= value <= 1.0:
        raise JevResponseError(f"relevance score {value} outside [0, 1]")
    return value


@dataclass
class JevClient:
    """Per-pair relevance scoring against the Jev API, with an on-disk cache.

    One request per (query, memory) pair, the pattern the public reranking
    examples use. If the API turns out to accept many questions per call
    against one state, batching a query's candidates into one request is the
    obvious next change; keep it behind this same `score` signature."""

    api_key: str
    api_url: str
    model: str | None = None
    concurrency: int = 8
    timeout_s: float = 30.0
    cache_path: pathlib.Path | None = None
    name: str = "jev"
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    _cache: dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_env(cls, cache_path: pathlib.Path | None = None) -> JevClient:
        key = os.environ.get("JEV_API_KEY")
        url = os.environ.get("JEV_API_URL")
        missing = [n for n, v in (("JEV_API_KEY", key), ("JEV_API_URL", url)) if not v]
        if missing:
            raise JevConfigError(
                f"set {' and '.join(missing)} to use --scorer jev "
                f"(or run with --scorer lexical for the offline control arm)")
        client = cls(api_key=key, api_url=url, model=os.environ.get("JEV_MODEL") or None,
                     concurrency=int(os.environ.get("JEV_CONCURRENCY", "8")),
                     timeout_s=float(os.environ.get("JEV_TIMEOUT_S", "30")),
                     cache_path=cache_path)
        client._load_cache()
        return client

    def score(self, query: str, candidates: list[tuple[str, str]]) -> dict[str, float]:
        with ThreadPoolExecutor(max_workers=max(1, self.concurrency)) as pool:
            scores = pool.map(lambda c: self._score_one(query, c[1]), candidates)
            return {nid: s for (nid, _), s in zip(candidates, scores)}

    def _cache_key(self, query: str, memory: str) -> str:
        raw = json.dumps([self.model, RELEVANCE_QUESTION, query, memory])
        return hashlib.sha256(raw.encode()).hexdigest()

    def _score_one(self, query: str, memory: str) -> float:
        key = self._cache_key(query, memory)
        with self._lock:
            if key in self._cache:
                self.cache_hits += 1
                return self._cache[key]
        value = _parse_score(self._post(_request_body(query, memory, self.model)))
        with self._lock:
            self.calls += 1
            self.input_tokens += estimate_tokens(query) + estimate_tokens(memory)
            self._cache[key] = value
            if self.cache_path is not None:
                with self.cache_path.open("a") as f:
                    f.write(json.dumps({"k": key, "v": value}) + "\n")
        return value

    def _post(self, body: dict) -> dict:
        # STUB (auth header): Bearer is the common convention; confirm in the docs.
        req = urllib.request.Request(
            self.api_url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode(errors="replace")
            raise JevResponseError(f"Jev HTTP {exc.code}: {detail}") from exc

    def _load_cache(self) -> None:
        if self.cache_path is None or not self.cache_path.exists():
            return
        for line in self.cache_path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                self._cache[row["k"]] = row["v"]
