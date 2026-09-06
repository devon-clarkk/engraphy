"""Providers behind the `LLMClient` protocol (design/09 §Module layout).

Three routes, and which one a run uses is the difference between an internal
measurement and a reproducible one.

**`OpenAICompatClient` is the route for anyone reproducing a published number.**
It speaks `POST /chat/completions` to any OpenAI-shaped endpoint, so a full
LoCoMo run needs a base URL and an API key and nothing else: no subscription, no
vendor CLI, no free-tier daily cap to schedule around. `bench/RUN-LOCOMO.md` is
the walkthrough, and `--provider openai` on `bench.core.run` is the switch.

The other two are free routes for the operator's own machine:

* **Claude via the CLI in print mode** — extractor and reader. Routes through the
  operator's existing subscription, not API credits. The extractor decides what
  enters memory, so it gets the strongest model available for free.
* **Gemini free tier** — judge.

**Cross-vendor judging is a neutrality win, not a cost workaround.** A harness
whose judge is the same vendor as the system under test invites the obvious
objection that it graded its own homework. Grading Engraphy's answers with a
different vendor's model removes that objection outright, and it is worth
keeping even if funding later appears. Recorded as such in design/09.

## Two rules the Claude CLI route exists to enforce

**Never introduce ANTHROPIC_API_KEY.** Setting it — even transiently in the
process environment — would make Claude Code itself bill to API credits rather
than the subscription. The CLI subprocess is launched with that variable
actively *stripped* from its environment, so a stray global on the operator's
machine cannot silently start charging.

**Read GEMINI_API_KEY from the repo's gitignored `.env`, not the environment.**
Explicitly, by path. A process-wide variable is exactly the kind of ambient
state that leaks into unrelated tools; a file read is auditable and scoped.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import tempfile
import subprocess
import time
import urllib.error
import urllib.request

from bench.core.llm import DEFAULT_MAX_TOKENS, LLMError, LLMResponse

__all__ = [
    "CLAUDE_DEFAULT_MODEL",
    "ENV_PATH",
    "GEMINI_DEFAULT_MODEL",
    "GEMINI_FREE_RPD",
    "GEMINI_FREE_RPM",
    "OPENAI_DEFAULT_BASE_URL",
    "OPENAI_DEFAULT_STRUCTURED",
    "OPENAI_STRUCTURED_MODES",
    "OPENAI_TOOL_NAME",
    "ClaudeCLIClient",
    "GeminiClient",
    "OpenAICompatClient",
    "QuotaExhausted",
    "TransientCLIError",
    "TransientRunStop",
    "read_env_file",
    "stop_class",
]

ENV_PATH = pathlib.Path(__file__).resolve().parents[2] / ".env"

CLAUDE_DEFAULT_MODEL = "sonnet"
GEMINI_DEFAULT_MODEL = "gemini-flash-latest"

# Verified free-tier ceilings (2026-07). Pro models were removed from the free
# tier in April 2026, so Flash / Flash-Lite are the only options.
#
# **The daily cap is per project PER MODEL, and it is not 1,500 for every model.**
# Measured live 2026-07-22 by exhausting it mid-run: `gemini-flash-latest`
# (resolving to gemini-3.6-flash) allows **20 requests/day** --
# quotaId `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, quotaValue 20.
# The harness had 1,500 here and a smoke test that made four calls, so nothing
# ever contradicted the assumption until a run needed 858 grades.
#
# RPD below is therefore a *client-side guard*, not the authority. The authority
# is the server's 429, which `_post` now classifies on the full response body.
GEMINI_FREE_RPM = 10
GEMINI_FREE_RPD = 1500
# What each model actually allowed when measured. Recorded so the next person
# picking a judge model does not have to rediscover it by burning a run.
GEMINI_MEASURED_RPD = {
    "gemini-flash-latest": 20,        # gemini-3.6-flash -- unusable for a benchmark
    "gemini-2.0-flash": 0,            # daily cap already exhausted when probed
}
# Verified live 2026-07-22, and NOT what the docs imply: `gemini-2.5-flash`
# returns 404 "no longer available to new users" on generateContent. The
# rolling `-latest` aliases are what a new key can actually reach, so those are
# what the harness pins. Nastier still, the dead id answers `countTokens`
# perfectly well -- so a construction-time or token-counting check passes and
# only the first real generation fails.
GEMINI_FREE_MODELS = (
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
)


class QuotaExhausted(LLMError):
    """The daily free-tier allowance is gone.

    Distinct from a transient failure on purpose: a run that hits this should
    stop cleanly and resumably, with partial results kept, rather than retrying
    into a wall or crashing halfway through and losing the work already done.
    """


class TransientCLIError(LLMError):
    """A CLI *launch* failed in a way that typically self-heals within
    seconds-to-minutes.

    A Claude Code self-update replaces `claude.exe` in place, so a launch can
    momentarily fail with FileNotFoundError (binary absent) or PermissionError
    (binary locked while being written). This is neither a usage cap
    (`QuotaExhausted` -> sleep to the reset) nor a genuine bug (plain `LLMError`
    -> halt): the right response is to re-resolve the binary and retry with
    backoff, and above that to relaunch. Kept a distinct type so every layer can
    tell the three cases apart.
    """


class TransientRunStop(LLMError):
    """A cycle stopped cleanly for a self-healing reason, NOT a usage cap.

    Raised when the answer phase's breaker trips on a run of `TransientCLIError`s
    (the CLI could not be launched for a stretch). Clean and resumable exactly
    like `QuotaExhausted` -- no failed answer is ever checkpointed -- but flagged
    distinctly so the supervisor relaunches after a short backoff rather than
    sleeping to a usage reset.
    """


def stop_class(reason: str) -> str:
    """Bucket a clean-stop reason string into how the SUPERVISOR should respond.

    `transient` -> a self-healing CLI-launch outage; back off briefly and
    relaunch. `usage` -> a subscription/quota cap; sleep to the reset. `halt` ->
    anything else (a genuine, repeating failure); stop for manual attention
    rather than spinning. The run emits the resulting class as a terminal
    `[stop] class=...` line, which is the authority the supervisor reads -- prose
    left earlier in the log by a blip that later recovered must not reroute a
    real stop.
    """
    r = (reason or "").lower()
    if ("transientclierror" in r or "cli could not be launched" in r
            or "the cli was not found" in r):
        return "transient"
    if any(s in r for s in (
        "usage limit", "quota", "429", "out of usage", "out of extra usage",
        "rate limit", "resets", "limit reached", "limit exceeded",
    )):
        return "usage"
    return "halt"


# A Claude Code self-update swaps claude.exe in place, so a launch can fail for a
# few seconds. Re-resolve and back off within this PER-CALL budget; a longer
# outage is handed up as TransientCLIError, and the supervisor carries the long
# tail with its own bounded backoff-relaunch. Kept modest on purpose: an
# 8-consecutive breaker times this budget, so a large per-call value would
# balloon a cycle's wall-clock before it even stops.
_CLI_LAUNCH_RETRY_BUDGET_S = 75.0


def read_env_file(key: str, path: pathlib.Path | None = None) -> str:
    """Read one key from the repo's gitignored `.env`.

    Deliberately not `os.environ`: the operator keeps this key in a file, and a
    process-wide variable would leak into every subprocess the harness spawns —
    including the Claude CLI, where unrelated credentials have real consequences.
    """
    # Fully resolved, always. There are two copies of this repo on this machine
    # (a live one and a stale OneDrive mirror), and a key saved into the wrong
    # one is indistinguishable from a key never saved unless the error names the
    # exact file that was read. That ambiguity cost a round trip once already.
    path = (path or ENV_PATH).resolve()
    if not path.exists():
        raise LLMError(
            f"{path} does not exist. The harness reads {key} from that file "
            "(not from the environment). Create it with a line: "
            f"{key}=your-key-here"
        )
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            value = value.strip().strip('"').strip("'")
            if not value:
                raise LLMError(f"{key} is present in {path} but empty")
            return value
    raise LLMError(
        f"{key} not found in {path} (that exact file -- check you did not edit a "
        f"different copy of the repo). Add a line: {key}=your-key-here"
    )


def _looks_like_usage_limit(text: str) -> bool:
    """Does a CLI failure look like a subscription usage cap rather than a bug?

    Matched on the phrases the CLI actually surfaces for a plan cap or an HTTP
    429. Deliberately does NOT match "overloaded" (a 529): that is a transient
    server condition, not a usage cap, and it should be retried through the
    normal error path rather than ending the run -- though even if it did end
    the run, the stop would still be clean and resumable, so erring toward
    stopping is safe. A cap misread as a transient error is the costly
    direction: it would be recorded as a wrong answer.
    """
    t = (text or "").lower()
    return any(s in t for s in (
        "usage limit", "rate limit", "rate_limit", "too many requests",
        "429", "quota", "limit reached", "limit exceeded",
    ))


def _resolve_binary(name: str) -> list[str]:
    """Find a form of the CLI that `subprocess` can actually launch with argv.

    Three live failures drove this, in order:

    1. A bare "claude" raises FileNotFoundError on Windows -- npm installs a
       `.cmd` shim plus an extensionless shell script; bash finds the script,
       CreateProcess finds neither.
    2. The `.cmd` shim goes through cmd.exe, which mangles multi-line argv --
       the CLI reported "your message came through empty" and fell back to
       plain text, ignoring --output-format.
    3. Moving the prompt to STDIN fixed that but broke --json-schema: no
       structured_output is returned at all on that path.

    So the requirement is a *real executable* taking argv. Preference order:
    the packaged `claude.exe`, then any PATH executable that is not a shim,
    then node running the package's own wrapper.
    """
    # Only fall back to the packaged install for the default name. An explicit
    # binary must be honoured or a caller asking for a nonexistent one silently
    # gets the real CLI -- which is how the missing-binary error path passed
    # while testing nothing.
    if name == "claude":
        packaged = (
            pathlib.Path(os.environ.get("APPDATA", ""))
            / "npm" / "node_modules" / "@anthropic-ai" / "claude-code"
        )
        exe = packaged / "bin" / "claude.exe"
        if exe.exists():
            return [str(exe)]
        wrapper = packaged / "cli-wrapper.cjs"
        node = shutil.which("node")
        if wrapper.exists() and node:
            return [node, str(wrapper)]

    found = shutil.which(name)
    if found and not found.lower().endswith((".cmd", ".bat")):
        return [found]

    return [found or name]


class ClaudeCLIClient:
    """Claude through `claude -p`, on the operator's subscription.

    Observed CLI contract (verified live 2026-07-21, not taken from docs):

    * `--output-format json` returns one JSON object per call.
    * With `--json-schema`, the parsed object arrives under `structured_output`
      and **`result` is empty** — reading `result` alone silently yields nothing.
    * `usage.input_tokens` excludes a large `cache_creation_input_tokens` for
      the CLI's own injected system prompt (~9-15k tokens even for a 3-token
      user turn), and `modelUsage` shows a second model (Haiku) running
      alongside the requested one for internal work.

    That last point is why **CLI usage numbers are not used for the headline
    token metric**: they measure the CLI's overhead, not the memory payload the
    agent was handed. `count_tokens` here raises rather than returning a number
    that would look like a measurement.
    """

    provider = "claude-cli"

    def __init__(
        self,
        model: str = CLAUDE_DEFAULT_MODEL,
        *,
        timeout: int = 300,
        binary: str = "claude",
    ) -> None:
        self.model = model
        self.timeout = timeout
        # Keep the requested NAME so the binary can be re-resolved at call time:
        # a self-update may move or restore claude.exe between calls, so pinning
        # the resolved path once in __init__ would keep launching a stale path.
        self._binary_name = binary
        self.binary = _resolve_binary(binary)
        # Run from an empty scratch directory, NOT the repo. Verified live: with
        # the repo as cwd the CLI auto-discovers CLAUDE.md and the project's
        # memory, and answered a reader prompt with prose about "this project's
        # memory file" instead of the requested JSON. That is a correctness bug
        # (non-JSON output) and a neutrality bug (harness model calls must not
        # see the repo's own instructions), so the cwd is isolated at source.
        self.workdir = pathlib.Path(tempfile.gettempdir()) / "engraphy-bench-cli"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.last_model_usage: dict = {}

    def _env(self) -> dict:
        """Subprocess environment with Anthropic API credentials stripped.

        If ANTHROPIC_API_KEY reaches the CLI it authenticates by API key and
        bills credits instead of the subscription — the precise outcome this
        routing exists to avoid. Stripped rather than merely not-set, because
        the parent process may have inherited one.
        """
        env = dict(os.environ)
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(var, None)
        return env

    def _launch(
        self, rest: list[str], stdin_text: str | None = None
    ) -> subprocess.CompletedProcess:
        """Run the CLI, tolerating a transiently-missing binary.

        A Claude Code self-update replaces claude.exe in place, so the launch can
        fail with FileNotFoundError (absent) or PermissionError (locked mid-write)
        for a few seconds. Rather than surfacing that as a wrong answer, re-resolve
        the binary each attempt -- the update may have restored or moved it -- and
        back off within `_CLI_LAUNCH_RETRY_BUDGET_S`. Beyond the budget, raise
        `TransientCLIError` so the answer phase stops cleanly and the supervisor
        relaunches. A process that DID launch and then hung (TimeoutExpired) is a
        different failure and is deliberately NOT retried here.

        `stdin_text` carries the prompt when it is too large for argv (see
        `complete`). Windows caps a command line at ~32 KB, and a large
        traverse envelope passed as an argument raises WinError 206 -- which is
        DETERMINISTIC, not a transient swap, so it is re-raised immediately as a
        clear error instead of being retried into a `TransientCLIError` that
        would spin the supervisor forever.
        """
        deadline = time.monotonic() + _CLI_LAUNCH_RETRY_BUDGET_S
        delay = 3.0
        attempt = 0
        last_exc: OSError | None = None
        while time.monotonic() < deadline or attempt == 0:
            attempt += 1
            # Fresh resolution each attempt: a stale pinned path would keep
            # launching a binary the self-update already moved.
            binary = _resolve_binary(self._binary_name)
            try:
                return subprocess.run(
                    [*binary, *rest], input=stdin_text, capture_output=True, text=True,
                    timeout=self.timeout, env=self._env(),
                    encoding="utf-8", errors="replace", cwd=self.workdir, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise LLMError(f"claude CLI timed out after {self.timeout}s") from exc
            except OSError as exc:
                # WinError 206 = command line too long. Retrying cannot fix a
                # prompt that will not fit in argv, and misfiling it as transient
                # is what spun a whole arm on backoff. Fail loudly and specifically.
                if getattr(exc, "winerror", None) == 206:
                    raise LLMError(
                        "the CLI command line exceeded the OS limit (WinError 206): the "
                        f"prompt is too large for argv ({sum(len(a) for a in rest)} chars). "
                        "Large prompts must route through stdin -- a schema-constrained call "
                        "cannot, so its prompt must be reduced."
                    ) from exc
                last_exc = exc
                if time.monotonic() + delay >= deadline:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 20.0)
        raise TransientCLIError(
            f"the CLI could not be launched ({self._binary_name}) after {attempt} "
            f"attempt(s) over ~{_CLI_LAUNCH_RETRY_BUDGET_S:.0f}s: {last_exc}. Likely "
            "a Claude Code self-update swapping claude.exe; the harness routes "
            "Claude through the CLI (subscription), not the API."
        ) from last_exc

    def complete(
        self,
        system: str,
        user: str,
        *,
        schema: dict | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = "high",
    ) -> LLMResponse:
        # Transport depends on whether structured output is needed, because the
        # two CLI mechanisms are not equivalent and each has one fatal edge:
        #
        #  * argv preserves --json-schema (STDIN silently disables it, so
        #    extraction/adjudication/judging would produce nothing), but a
        #    Windows command line caps at ~32 KB -- a large reader envelope on
        #    argv raises WinError 206.
        #  * STDIN has no length limit but disables --json-schema.
        #
        # So: schema calls go on argv (their prompts are small -- a turn window,
        # a few candidates, one verdict). Schema-less calls -- the reader, whose
        # traverse envelopes routinely exceed 32 KB -- go on STDIN. This is the
        # fix for the search_then_traverse arm dying on WinError 206.
        use_stdin = schema is None
        rest = [
            "-p",
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", system,
            # The harness calls the model, not an agent: every tool is denied so
            # a prompt cannot cause file or network access, and so the run stays
            # a measurement of the model rather than of Claude Code.
            "--disallowed-tools", "Bash Edit Write Read Glob Grep WebFetch WebSearch",
            "--strict-mcp-config",
        ]
        if not use_stdin:
            rest.insert(1, user)  # prompt as the argument to -p
            rest += ["--json-schema", json.dumps(schema)]

        started = time.perf_counter()
        proc = self._launch(rest, stdin_text=user if use_stdin else None)
        elapsed = time.perf_counter() - started

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            # A subscription usage cap must be the clean, resumable stop -- the
            # same contract the Gemini daily quota gets -- not a generic failure
            # that a caller records as a wrong answer. When the judge routes
            # through this client (see run.py --judge claude), a mid-run cap is
            # exactly the QuotaExhausted path: stop, keep every checkpoint,
            # resume later.
            if _looks_like_usage_limit(stderr) or _looks_like_usage_limit(stdout):
                raise QuotaExhausted(
                    f"Claude CLI usage limit reached: {(stderr or stdout)[:400]}")
            # Exit non-zero with NO diagnostic on either stream is the signature
            # of an auth / usage / rate cap: the CLI bails without a message.
            # Seen live (2026-07-23) -- five consecutive `exited 1` with empty
            # stderr at the START of a judge pass, right after a heavy 2,500-call
            # answer phase, was a Max-plan usage limit. Treat it as the clean,
            # resumable quota-style stop rather than a generic error, so a resume
            # picks up from the checkpoint instead of burning the breaker.
            if not stderr:
                raise QuotaExhausted(
                    f"Claude CLI exited {proc.returncode} with no diagnostic output "
                    f"(stdout: {stdout[:200]!r}). Treating as a usage/rate limit -- "
                    "clean resumable stop; rerun the same command to continue.")
            raise LLMError(f"claude CLI exited {proc.returncode}: {stderr[:400]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude CLI did not return JSON: {proc.stdout[:300]!r}") from exc

        if payload.get("is_error"):
            result = str(payload.get("result", ""))
            if _looks_like_usage_limit(result):
                raise QuotaExhausted(f"Claude CLI usage limit reached: {result[:400]}")
            raise LLMError(f"claude CLI reported an error: {result[:300]}")

        usage = payload.get("usage") or {}
        self.last_model_usage = payload.get("modelUsage") or {}
        data = payload.get("structured_output") if schema is not None else None
        if schema is not None and data is None:
            raise LLMError("--json-schema was requested but no structured_output was returned")

        return LLMResponse(
            # With a schema the CLI leaves `result` empty; carry the structured
            # object as the text so callers that only read `.text` still see it.
            text=payload.get("result") or (json.dumps(data) if data is not None else ""),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model=self._resolved_model(),
            stop_reason=payload.get("stop_reason") or "",
            data=data,
            seconds=elapsed,
        )

    def _resolved_model(self) -> str:
        """The model that actually did the work.

        `--model sonnet` is an alias; `modelUsage` names what ran. The manifest
        must record the resolved id, because the whole point of the pinning
        guard is that a cheaper model cannot silently move the headline.
        """
        if not self.last_model_usage:
            return self.model
        ranked = sorted(
            self.last_model_usage.items(),
            key=lambda kv: kv[1].get("outputTokens", 0),
            reverse=True,
        )
        return ranked[0][0]

    def count_tokens(self, text: str) -> int:
        raise LLMError(
            "the Claude CLI reports usage for its own injected system prompt "
            "(~9-15k cache-creation tokens per call) and cannot count an "
            "arbitrary payload. Token figures are omitted rather than estimated "
            "-- see design/09 §Token accounting."
        )


class GeminiClient:
    """Gemini free tier, over REST. The judge.

    stdlib `urllib` rather than a vendor SDK: one HTTP call with a JSON body
    does not justify a dependency the engine's install would have to carry, and
    it keeps the request shape visible at the call site where it can be audited.

    Rate limiting is built in because the free tier's ceilings are hard: ~10
    requests/minute and 1,500/day. Exceeding them is not a transient failure to
    retry through — the daily one ends the run, so it raises `QuotaExhausted`
    for a clean, resumable stop.
    """

    provider = "gemini"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(
        self,
        model: str = GEMINI_DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        rpm: int = GEMINI_FREE_RPM,
        rpd: int = GEMINI_FREE_RPD,
        timeout: int = 120,
        max_retries: int = 4,
    ) -> None:
        if model not in GEMINI_FREE_MODELS:
            raise LLMError(
                f"{model!r} is not on the Gemini free tier. Pro models were removed "
                f"from it in April 2026; choose one of {list(GEMINI_FREE_MODELS)}."
            )
        self.model = model
        self.api_key = api_key or read_env_file("GEMINI_API_KEY")
        self.rpm = rpm
        self.rpd = rpd
        self.timeout = timeout
        self.max_retries = max_retries
        self._minute_window: list[float] = []
        self.requests_today = 0

    # -- rate limiting ----------------------------------------------------
    def _throttle(self) -> None:
        if self.requests_today >= self.rpd:
            raise QuotaExhausted(
                f"Gemini free-tier daily limit reached ({self.rpd} requests). "
                "Stop here and resume tomorrow; partial results are kept."
            )
        now = time.monotonic()
        self._minute_window = [t for t in self._minute_window if now - t < 60.0]
        if len(self._minute_window) >= self.rpm:
            # Sleep only as long as the oldest request needs to age out.
            wait = 60.0 - (now - self._minute_window[0]) + 0.25
            if wait > 0:
                time.sleep(wait)
            now = time.monotonic()
            self._minute_window = [t for t in self._minute_window if now - t < 60.0]
        self._minute_window.append(now)
        self.requests_today += 1

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.ENDPOINT}/{self.model}:{path}?key={self.api_key}"
        data = json.dumps(body).encode("utf-8")
        delay = 2.0
        for attempt in range(self.max_retries):
            self._throttle()
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # Classify on the WHOLE body, then truncate for display. This
                # was the other way round and it silently broke the only
                # classification that matters: Google reports the daily cap in a
                # `quotaId` of "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
                # that sits ~700 bytes into the response, well past a 300-char
                # slice. So every daily-quota 429 was read as transient, retried
                # four times, and surfaced as a generic failure -- which a caller
                # then recorded as a wrong answer. Truncate for humans, never
                # before the machine has looked.
                body = exc.read().decode("utf-8", "replace")
                detail = body[:300]
                if exc.code == 429:
                    # A 429 that mentions the daily quota will never clear by
                    # waiting a few seconds -- distinguish it so the run stops
                    # rather than burning its retries.
                    if "PerDay" in body or "per day" in body.lower():
                        raise QuotaExhausted(
                            f"Gemini daily quota exhausted for {self.model}: {_quota_note(body)}"
                        ) from exc
                    if attempt == self.max_retries - 1:
                        raise LLMError(f"Gemini rate limited after retries: {detail}") from exc
                    time.sleep(delay)
                    delay *= 2
                    continue
                if exc.code >= 500 and attempt < self.max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"Gemini HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt == self.max_retries - 1:
                    raise LLMError(f"Gemini connection failed: {exc}") from exc
                time.sleep(delay)
                delay *= 2
        raise LLMError("Gemini request failed after retries")

    # -- protocol ---------------------------------------------------------
    def complete(
        self,
        system: str,
        user: str,
        *,
        schema: dict | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = "high",
    ) -> LLMResponse:
        gen: dict = {"maxOutputTokens": max_tokens, "temperature": 0}
        if schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = _to_gemini_schema(schema)

        body = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": gen,
        }
        started = time.perf_counter()
        payload = self._post("generateContent", body)
        elapsed = time.perf_counter() - started

        candidates = payload.get("candidates") or []
        if not candidates:
            raise LLMError(f"Gemini returned no candidates: {json.dumps(payload)[:300]}")
        cand = candidates[0]
        finish = cand.get("finishReason", "")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if finish == "MAX_TOKENS":
            raise LLMError(f"Gemini hit maxOutputTokens ({max_tokens}); response truncated")
        if finish == "SAFETY":
            raise LLMError("Gemini declined the request (finishReason=SAFETY)")

        data = None
        if schema is not None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LLMError(f"Gemini schema response was not valid JSON: {text[:200]!r}") from exc

        usage = payload.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            model=payload.get("modelVersion") or self.model,
            stop_reason=finish,
            data=data,
            seconds=elapsed,
        )

    def count_tokens(self, text: str) -> int:
        """Gemini's own tokenizer.

        Valid for Gemini-side accounting (what the judge consumed). It must NOT
        be used for the reader-payload headline metric: a Gemini count of a
        payload a Claude model reads is a different tokenizer measuring
        somebody else's work, which is the same objection that ruled out
        tiktoken.
        """
        payload = self._post("countTokens", {"contents": [{"parts": [{"text": text}]}]})
        return int(payload.get("totalTokens") or 0)


def _quota_note(body: str) -> str:
    """Pull the quota id and value out of a 429 body for the error message.

    The numbers matter to whoever reads the failure: "20 requests/day" and
    "1,500 requests/day" call for completely different plans, and the raw body
    buries both behind 700 bytes of documentation links.
    """
    import re

    qid = re.search(r'"quotaId":\s*"([^"]+)"', body)
    val = re.search(r'"quotaValue":\s*"(\d+)"', body)
    delay = re.search(r'"retryDelay":\s*"([^"]+)"', body)
    parts = []
    if val:
        parts.append(f"limit {val.group(1)}")
    if qid:
        parts.append(qid.group(1))
    if delay:
        parts.append(f"retry after {delay.group(1)}")
    return "; ".join(parts) or body[:200]


def _to_gemini_schema(schema: dict) -> dict:
    """Translate a JSON Schema to Gemini's OpenAPI-subset `responseSchema`.

    Gemini rejects `additionalProperties` and `$`-prefixed keywords, so they are
    stripped recursively rather than passed through and 400ing at request time.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    for key, value in schema.items():
        if key in ("additionalProperties", "$schema", "$defs", "$ref", "propertyOrdering"):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _to_gemini_schema(v) for k, v in value.items()}
        elif key == "items":
            out[key] = _to_gemini_schema(value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------- OpenAI-compatible
# The third-party reproduction route (bench/RUN-LOCOMO.md).
#
# The two clients above are free-tier routes built for one machine: one needs a
# Claude subscription and the `claude` binary, the other a Gemini key whose free
# allowance is measured in hundreds of calls a day. Neither is something a
# reviewer can supply, and a result nobody else can re-run is a self-reported
# result whatever the documentation says.
#
# `OpenAICompatClient` is the route that closes that: one `POST /chat/completions`
# against any endpoint speaking the OpenAI shape -- OpenAI itself, Anthropic's
# OpenAI-compatibility layer, Google's, a gateway such as OpenRouter or LiteLLM,
# or a local vLLM. Every role can route through it, so a full LoCoMo run needs a
# base URL and a key and nothing else.
#
# stdlib `urllib`, for the same reason `GeminiClient` uses it: one HTTP call with
# a JSON body does not justify a dependency, and the `bench` extra stays a single
# package so `bench/tests` keeps running in CI with nothing extra installed.

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"

# How a schema-constrained call asks for structured output. Three mechanisms,
# because endpoints implement different ones and picking the wrong one is silent:
#
# * `json_schema` -- `response_format: {"type": "json_schema", ...}`. What OpenAI
#   itself implements and what most gateways pass through. The default.
# * `tool_call` -- a single forced function call whose `parameters` carry the
#   schema; the arguments are read back as the payload. Required for
#   **Anthropic's OpenAI-compatibility layer, which ignores `response_format`**
#   (its published support table lists the field as ignored, while
#   `tools[n].function.parameters` and `tool_calls` are fully supported). An
#   endpoint that ignores the field does not error: it returns prose, and the
#   role that needed JSON records a failure. So this is a setting an operator
#   chooses from the table in bench/RUN-LOCOMO.md, not something to guess at.
# * `json_object` -- `response_format: {"type": "json_object"}` plus the schema
#   appended to the system prompt. The lowest common denominator, for shims that
#   implement neither of the above. Weaker on purpose: the shape is asked for
#   rather than enforced, so a run using it says so in the manifest.
OPENAI_STRUCTURED_MODES = ("json_schema", "tool_call", "json_object")
OPENAI_DEFAULT_STRUCTURED = "json_schema"

# The function name used by `tool_call` mode. Fixed rather than generated so a
# request is reproducible byte for byte.
OPENAI_TOOL_NAME = "emit_result"

# `strict: true` is NOT set on the json_schema mode, and that is deliberate.
# OpenAI's strict mode requires every key in `properties` to appear in
# `required`, and the harness's extraction schema deliberately leaves `attrs`,
# `supersedes_title` and `source_turn_ids` optional -- a memory carrying no typed
# attributes is a legitimate extraction. Turning strict on would force all three
# onto every memory, which changes what the extractor produces and therefore what
# the run measures. Conformance is checked where it already was:
# `validate_against_pack`, and the engine's own attr-spec interpreter behind it.
OPENAI_STRICT_SCHEMA = False


def _setting(name: str, default: str = "") -> str:
    """One harness setting, from the environment first and the repo `.env` second.

    Both, unlike `read_env_file`, which is file-only. Someone running this from a
    CI job or a container has no `.env` and should not be made to write one; the
    operator who already keeps credentials in `.env` should not be made to export
    them. Environment wins, so a one-off override needs no file edit.
    """
    from_env = os.environ.get(name)
    if from_env and from_env.strip():
        return from_env.strip()
    try:
        from_file = read_env_file(name)
    except LLMError:
        return default
    # An empty resolved value is a miss, not a setting. `_setting` feeds callers
    # that parse what they get (`int(...)`, `float(...)`), and handing them ""
    # turns an unset variable into a crash rather than a default.
    return from_file.strip() or default


class OpenAICompatClient:
    """Any OpenAI-shaped `/chat/completions` endpoint, behind the `LLMClient` seam.

    Configuration is per role and read from the environment (or `.env`), so a run
    can put the reader on one endpoint and the judge on another. That separation
    is the point: design/09's cross-vendor judge rule is what keeps a published
    number off a harness that graded its own vendor's answers, and collapsing
    both roles onto one variable would make the neutral posture unreachable.

        ENGRAPHY_BENCH_OPENAI_BASE_URL       reader / extractor / adjudicator
        ENGRAPHY_BENCH_OPENAI_API_KEY
        ENGRAPHY_BENCH_OPENAI_STRUCTURED     json_schema | tool_call | json_object

        ENGRAPHY_BENCH_JUDGE_BASE_URL        the judge; falls back to the above
        ENGRAPHY_BENCH_JUDGE_API_KEY
        ENGRAPHY_BENCH_JUDGE_STRUCTURED

    Model ids are pinned in `bench.core.llm.OPENAI_ROLE_MODELS` and overridable
    per role from there, not here, so the manifest keeps one authority for what
    ran.

    ## Three request-shape facts this client is built around

    **No `temperature` unless asked for.** Reasoning-tier models on several
    endpoints reject any value but the default, and the harness does not assume
    determinism anywhere (see the `bench/core/llm.py` module docstring) -- judge
    stability is measured on every run instead. Set
    `ENGRAPHY_BENCH_OPENAI_TEMPERATURE` to send one.

    **`max_tokens` first, `max_completion_tokens` on demand.** The former is what
    every shim accepts; the latter is what OpenAI's newer models require. A 400
    naming both is unambiguous and fixed by renaming one key, so the client
    retries on the other field and remembers the answer for the rest of the run.

    **Token counts are not offered.** `count_tokens` raises, matching
    `ClaudeCLIClient`. The retrieval-envelope metric is reported in bytes exactly
    because no tokenizer available here measures the model that reads the
    payload, and a client that started returning numbers would quietly contradict
    the manifest's `token_counter_note`.
    """

    provider = "openai-compat"

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "",
        api_key: str = "",
        structured: str = "",
        timeout: int = 300,
        max_retries: int = 4,
        temperature: str = "",
        rpm: int = 0,
        role: str = "",
        opener=None,
    ) -> None:
        self.model = model
        self.role = role
        self.base_url = (base_url or OPENAI_DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        self.structured = structured or OPENAI_DEFAULT_STRUCTURED
        if self.structured not in OPENAI_STRUCTURED_MODES:
            raise LLMError(
                f"{self.structured!r} is not a structured-output mode; choose one of "
                f"{list(OPENAI_STRUCTURED_MODES)}. bench/RUN-LOCOMO.md lists which one "
                "each endpoint needs."
            )
        if not self.api_key:
            raise LLMError(
                f"no API key for the {role or 'openai-compat'} route. Set "
                "ENGRAPHY_BENCH_OPENAI_API_KEY (or ENGRAPHY_BENCH_JUDGE_API_KEY for the "
                "judge) in the environment or the repo's gitignored .env."
            )
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.rpm = rpm
        # Injected by tests so the whole request and response path runs with no
        # network. Production leaves it None and uses urllib.
        self._opener = opener
        self._minute_window: list[float] = []
        # Which output-cap field this endpoint accepts, learned on first refusal.
        self._max_tokens_field = "max_tokens"
        self.last_response_model = ""

    @classmethod
    def for_role(cls, role: str, model: str, **kwargs) -> OpenAICompatClient:
        """Build the client for one role from the environment.

        The judge reads its own three variables and falls back to the shared ones
        when they are unset. `run.py` records which of those happened, so a run
        whose judge shares an endpoint with its reader states that in the
        manifest rather than leaving a reader to infer it from two matching
        hostnames.
        """
        prefix = "ENGRAPHY_BENCH_JUDGE_" if role == "judge" else "ENGRAPHY_BENCH_OPENAI_"
        base_url = _setting(prefix + "BASE_URL") or _setting("ENGRAPHY_BENCH_OPENAI_BASE_URL")
        api_key = _setting(prefix + "API_KEY") or _setting("ENGRAPHY_BENCH_OPENAI_API_KEY")
        structured = (_setting(prefix + "STRUCTURED")
                      or _setting("ENGRAPHY_BENCH_OPENAI_STRUCTURED"))
        rpm_raw = _setting("ENGRAPHY_BENCH_OPENAI_RPM", "0")
        try:
            rpm = int(rpm_raw)
        except ValueError:
            raise LLMError(
                f"ENGRAPHY_BENCH_OPENAI_RPM must be an integer, got {rpm_raw!r}"
            ) from None
        return cls(
            model,
            base_url=base_url,
            api_key=api_key,
            structured=structured,
            temperature=_setting("ENGRAPHY_BENCH_OPENAI_TEMPERATURE"),
            rpm=rpm,
            role=role,
            **kwargs,
        )

    # -- config surface, for the manifest ---------------------------------
    def describe(self) -> dict:
        """What this client is, for the run manifest.

        The host, never the key. A reviewer needs to know which vendor graded the
        answers; nobody needs the credential.
        """
        from urllib.parse import urlparse

        return {
            "provider": self.provider,
            "model": self.model,
            "base_url_host": urlparse(self.base_url).netloc or self.base_url,
            "structured_output": self.structured,
            "temperature": self.temperature or "endpoint default (unset)",
        }

    # -- rate limiting ----------------------------------------------------
    def _throttle(self) -> None:
        """Client-side requests-per-minute guard, off unless configured.

        Unlike Gemini's free tier, the ceiling here belongs to the operator's own
        account and the harness cannot know it. `ENGRAPHY_BENCH_OPENAI_RPM` lets a
        low-tier key pace itself rather than discover the limit as a wall of 429s
        partway through a 500-question run.
        """
        if self.rpm <= 0:
            return
        now = time.monotonic()
        self._minute_window = [t for t in self._minute_window if now - t < 60.0]
        if len(self._minute_window) >= self.rpm:
            wait = 60.0 - (now - self._minute_window[0]) + 0.25
            if wait > 0:
                time.sleep(wait)
            now = time.monotonic()
            self._minute_window = [t for t in self._minute_window if now - t < 60.0]
        self._minute_window.append(now)

    # -- transport --------------------------------------------------------
    def _open(self, req, timeout):
        if self._opener is not None:
            return self._opener(req, timeout)
        return urllib.request.urlopen(req, timeout=timeout)

    def _post(self, body: dict) -> dict:
        url = self.base_url + "/chat/completions"
        delay = 2.0
        for attempt in range(self.max_retries):
            self._throttle()
            payload = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.api_key,
                },
                method="POST",
            )
            try:
                with self._open(req, self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # Classify on the WHOLE body before truncating for display -- the
                # lesson `GeminiClient._post` already carries: the field that
                # separates "wait a moment" from "this run is over" sits hundreds
                # of bytes into the response.
                raw = exc.read().decode("utf-8", "replace")
                detail = raw[:300]
                if exc.code == 400 and self._switch_max_tokens_field(raw, body):
                    # Deterministic and fixable, so it does not consume a retry.
                    continue
                if exc.code in (401, 403):
                    raise LLMError(
                        f"the {self.role or 'openai-compat'} endpoint rejected the "
                        f"credential (HTTP {exc.code}): {detail}"
                    ) from exc
                if exc.code == 429:
                    if _looks_like_hard_quota(raw):
                        raise QuotaExhausted(
                            f"{self.role or 'openai-compat'} quota exhausted on "
                            f"{self.model}: {detail}"
                        ) from exc
                    if attempt == self.max_retries - 1:
                        raise LLMError(
                            f"rate limited after {self.max_retries} attempts: {detail}"
                        ) from exc
                    time.sleep(_retry_after(exc.headers, delay))
                    delay *= 2
                    continue
                if exc.code >= 500 and attempt < self.max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"HTTP {exc.code} from {self.base_url}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt == self.max_retries - 1:
                    raise LLMError(f"connection to {self.base_url} failed: {exc}") from exc
                time.sleep(delay)
                delay *= 2
        raise LLMError(f"request to {self.base_url} failed after {self.max_retries} attempts")

    def _switch_max_tokens_field(self, raw: str, body: dict) -> bool:
        """Move the output cap to the other field name, once, in place.

        OpenAI's newer models refuse `max_tokens` and name `max_completion_tokens`
        in the refusal; several self-hosted shims do the exact reverse. Both are
        unambiguous, both are fixed by renaming one key, and neither is worth a
        flag the operator has to discover from a failed run. Returns True when it
        rewrote `body`, so the caller retries without spending an attempt.
        """
        lowered = raw.lower()
        current = self._max_tokens_field
        other = "max_completion_tokens" if current == "max_tokens" else "max_tokens"
        if other not in lowered or current not in body:
            return False
        body[other] = body.pop(current)
        self._max_tokens_field = other
        return True

    # -- protocol ---------------------------------------------------------
    def complete(
        self,
        system: str,
        user: str,
        *,
        schema: dict | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = "high",
    ) -> LLMResponse:
        # `effort` is accepted and not sent. It is an Anthropic-native concept
        # with no OpenAI-shaped equivalent every endpoint honours, and a field
        # silently dropped by the server is worse than one never sent: the
        # manifest would name a setting that did nothing.
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            self._max_tokens_field: max_tokens,
        }
        if self.temperature:
            body["temperature"] = float(self.temperature)

        if schema is not None:
            if self.structured == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "engraphy_bench_result",
                        "schema": schema,
                        "strict": OPENAI_STRICT_SCHEMA,
                    },
                }
            elif self.structured == "tool_call":
                body["tools"] = [{
                    "type": "function",
                    "function": {
                        "name": OPENAI_TOOL_NAME,
                        "description": "Return the result as the arguments of this call.",
                        "parameters": schema,
                    },
                }]
                body["tool_choice"] = {
                    "type": "function",
                    "function": {"name": OPENAI_TOOL_NAME},
                }
            else:  # json_object
                body["response_format"] = {"type": "json_object"}
                # The shape is only asked for in this mode, so it has to reach
                # the model in the prompt or there is nothing to conform to.
                body["messages"][0]["content"] = (
                    system + "\n\nReply with JSON matching this schema, and nothing "
                    "else:\n" + json.dumps(schema)
                )

        started = time.perf_counter()
        payload = self._post(body)
        elapsed = time.perf_counter() - started
        return self._parse(payload, schema, max_tokens, elapsed)

    def _parse(self, payload: dict, schema: dict | None,
               max_tokens: int, elapsed: float) -> LLMResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"no choices in the response: {json.dumps(payload)[:300]}")
        choice = choices[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason") or ""

        if finish == "length":
            # Never accepted silently, for the reason `AnthropicClient` gives: a
            # truncated extraction is indistinguishable from a conversation that
            # held few facts, and would understate the store with nothing in the
            # result to show for it.
            raise LLMError(
                f"the response hit the output cap ({max_tokens}) and is truncated; "
                "raise max_tokens rather than accepting a partial result"
            )
        if finish == "content_filter":
            raise LLMError("the endpoint's content filter stopped the response")
        if message.get("refusal"):
            raise LLMError(f"the model declined the request: {str(message['refusal'])[:200]}")

        text = message.get("content") or ""
        data = None
        if schema is not None:
            if self.structured == "tool_call":
                calls = message.get("tool_calls") or []
                if not calls:
                    raise LLMError(
                        "structured output was requested in tool_call mode and the "
                        "endpoint returned no tool call. An endpoint that honours "
                        "response_format should run in json_schema mode instead."
                    )
                arguments = (calls[0].get("function") or {}).get("arguments") or ""
                try:
                    data = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise LLMError(
                        f"the tool call's arguments were not valid JSON: {arguments[:200]!r}"
                    ) from exc
                text = arguments
            else:
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise LLMError(
                        "structured output was requested and the reply was not valid "
                        f"JSON: {text[:200]!r}. An endpoint that ignores response_format "
                        "returns prose here -- Anthropic's compatibility layer does, and "
                        "wants the tool_call mode."
                    ) from exc

        usage = payload.get("usage") or {}
        # Read into a local before it goes anywhere near the instance attribute.
        # `phase_answer` shares ONE reader client across `--concurrency` threads,
        # so a served id stashed on `self` and read back a line later can be
        # another thread's by the time it is read -- the manifest would then
        # attribute a model to the wrong answer, which is the exact failure the
        # served-id recording exists to prevent. The attribute is kept for
        # callers that want the last id seen; the response never depends on it.
        served = str(payload.get("model") or "")
        self.last_response_model = served
        return LLMResponse(
            text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            # The id the endpoint says served the request, so the manifest records
            # what ran rather than what was asked for -- the same pinning
            # guarantee `ClaudeCLIClient._resolved_model` exists to give.
            model=served or self.model,
            stop_reason=finish,
            data=data,
            seconds=elapsed,
        )

    def count_tokens(self, text: str) -> int:
        raise LLMError(
            "this client does not count tokens. The retrieval envelope is reported in "
            "bytes because no tokenizer available to the harness measures the model "
            "that reads the payload -- see the manifest's token_counter_note."
        )


def _retry_after(headers, fallback: float) -> float:
    """Seconds to wait, taken from the response's own `Retry-After` where it gives one.

    An endpoint that states its backoff knows better than an exponential guess,
    and honouring it is the difference between clearing a rate limit and walking
    into it four more times. Capped so a malformed or punitive value cannot
    stall a run for an hour.
    """
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return fallback
    try:
        return max(0.0, min(float(value), 300.0))
    except (TypeError, ValueError):
        return fallback


def _looks_like_hard_quota(body: str) -> bool:
    """Is this 429 a spent allowance rather than a burst that will clear?

    The distinction decides whether the run stops cleanly with every checkpoint
    intact or burns its retries against a wall. Matched on the phrases endpoints
    actually use for an exhausted balance or a daily cap; a plain
    `rate_limit_exceeded` is deliberately NOT matched, because that one does clear
    by waiting.
    """
    t = (body or "").lower()
    return any(s in t for s in (
        "insufficient_quota", "exceeded your current quota", "billing",
        "per day", "perday", "daily limit", "credit balance", "out of credits",
    ))
