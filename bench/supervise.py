"""Auto-resume supervisor: a usage limit must PAUSE the run, never break it.

**Only the `claude-cli` route needs this.** On the OpenAI-compatible route
(`--provider openai`, see bench/RUN-LOCOMO.md) there is no usage window to sleep
to: a rate limit that will clear is retried in place against the endpoint's own
`Retry-After`, and an exhausted balance is already a clean resumable stop that
the same command picks up. Run `python -m bench.core.run` directly there.

Devon's key requirement (2026-07-24): the matrix run spans multiple Claude
usage windows, and a rate limit must not end it -- it should pause until the
limit resets and continue from the checkpoint, unattended.

This is a thin outer loop around `python -m bench.core.run`. Each cycle launches
one run/resume, which itself runs until it either COMPLETES or stops cleanly on a
usage limit (the harness checkpoints every question and treats a Claude
`QuotaExhausted` as a clean, resumable stop). When a cycle stops on a usage
limit, the supervisor:

  1. parses the reset time from the CLI's message ("... resets 5:50pm
     (Australia/Perth)"),
  2. sleeps until that wall-clock time + a ~10-minute buffer (a sleeping process
     uses no Claude usage -- only the CLI subprocess is limited, so the sleep
     genuinely clears the window),
  3. relaunches the identical command, which resumes from the checkpoint.

It loops until the run completes or a safety cap of cycles is hit, logging every
sleep (with the computed wake time) and every resume.

**Honest limitation:** this handles usage-limit pauses while the machine stays
ON. A machine power-off still needs a manual kick -- but the run is checkpointed,
so a kick loses nothing and re-enters the same loop.
"""

from __future__ import annotations

import argparse
import atexit
import datetime
import os
import pathlib
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Substrings that mark a Claude usage-limit stop, used only as a FALLBACK when a
# cycle's output carries no explicit `[stop] class=...` line (an older run, or a
# hard crash). The authority is the class line the run itself emits.
USAGE_SIGNS = (
    "out of extra usage", "out of usage", "usage limit", "usage/rate limit",
    "resets", "429", "quotaexhausted",
)

# The run declares its own stop class as a terminal line; this reads the LAST one.
_STOP_RE = re.compile(r"\[stop\]\s+class=(\w+)", re.IGNORECASE)

# A NON-complete cycle whose declared class is this backs off briefly and
# relaunches rather than sleeping to a usage reset -- a self-healing CLI-launch
# outage clears in seconds-to-minutes.
TRANSIENT_BACKOFF_CAP_S = 300.0     # ~5 min per backoff, per Devon's tolerance
MAX_TRANSIENT_RETRIES = 6           # before a persistent "transient" is treated as hard

# Guard: a parsed reset further out than this is discarded for the fixed fallback,
# so a mis-parsed time (e.g. a tz-less value bumped +24h) cannot strand the run.
MAX_PARSED_SLEEP_S = 12 * 3600

_RESET_RE = re.compile(
    r"reset(?:s|\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s*(?:\(([^)]+)\))?",
    re.IGNORECASE)


def _pid_alive(pid: int) -> bool:
    """Is a process with this PID currently running?

    Windows-aware because that is where this runs: `os.kill(pid, 0)` is not a
    reliable liveness probe on Windows, so use `tasklist`, which is. A POSIX
    fallback keeps the guard working if the harness is ever run elsewhere. On
    any doubt the answer is True (assume alive), because the cost of a false
    "dead" is the exact thing this guard exists to prevent -- two supervisors
    driving one run.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return True  # cannot tell -> assume alive, refuse to double-launch
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True
    return True


def acquire_singleton_lock(run_dir: pathlib.Path, log_path: pathlib.Path) -> bool:
    """Refuse to start if another live supervisor already owns this run.

    The lock is scoped to the run directory, so it catches exactly the danger
    that matters: a second supervisor pointed at the *same* run. The frozen
    session that this run was handed off from has a relaunch queued against this
    same `--run-dir`; if it ever unblocks and fires, it must find this lock,
    see a live PID, and exit rather than start a rival that would corrupt the
    checkpoint by answering the same questions twice.

    A lock whose PID is dead (a prior supervisor that was killed) is stale and
    is taken over. Returns True if the lock was acquired, False if a live owner
    holds it.
    """
    lock_path = run_dir / "supervisor.lock"
    if lock_path.exists():
        try:
            existing = lock_path.read_text(encoding="utf-8").strip()
            owner_pid = int(existing.splitlines()[0])
        except (OSError, ValueError, IndexError):
            owner_pid = -1
        if owner_pid > 0 and owner_pid != os.getpid() and _pid_alive(owner_pid):
            _log(log_path, f"REFUSING to start: a live supervisor (pid {owner_pid}) "
                           f"already holds {lock_path}. This is the singleton guard "
                           "working -- not an error. Exiting.")
            return False
        _log(log_path, f"taking over stale lock {lock_path} (previous pid "
                       f"{owner_pid} is not alive)")

    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    lock_path.write_text(f"{os.getpid()}\n{stamp}\n", encoding="utf-8")

    def _release() -> None:
        # Only remove the lock if we still own it -- never delete a lock another
        # supervisor legitimately took over after we (somehow) lost ours.
        try:
            if lock_path.exists():
                held = int(lock_path.read_text(encoding="utf-8").splitlines()[0])
                if held == os.getpid():
                    lock_path.unlink()
        except (OSError, ValueError, IndexError):
            pass

    atexit.register(_release)
    return True


def parse_reset(text: str) -> datetime.datetime | None:
    """Parse the next reset instant from a CLI usage message.

    Handles "... resets 5:50pm (Australia/Perth)", and the leaner variants the
    CLI also emits: a missing timezone (falls back to the machine's local zone),
    a 24-hour time with no am/pm ("resets 17:50"), and "reset at ...". Returns
    None when the time is too ambiguous to trust (a bare hour with neither am/pm
    nor minutes) or the zone is unknown, so the caller falls back to a fixed
    sleep. Low-stakes by design: the fixed fallback covers every miss, and the
    one variant seen live ("you've hit your org's monthly usage limit") carries
    no time at all.
    """
    m = _RESET_RE.search(text or "")
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower().replace(".", "")
    tzname = (m.group(4) or "").strip()
    # A bare hour with no am/pm and no minutes ("resets 5") is genuinely
    # ambiguous (5am vs 5pm, ~12h apart) -- refuse it rather than guess.
    if not ampm and m.group(2) is None:
        return None
    if ampm == "pm" and hh != 12:
        hh += 12
    if ampm == "am" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    if tzname:
        try:
            tz = ZoneInfo(tzname)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return None
    else:
        # No zone named: use the machine's own. Safer than guessing a zone, and
        # the +10m buffer plus the MAX_PARSED_SLEEP_S guard absorb DST edges.
        tz = datetime.datetime.now().astimezone().tzinfo
    now = datetime.datetime.now(tz)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return target


def classify_stop(tail: str) -> str:
    """How to respond to a NON-complete cycle: 'transient' | 'usage' | 'halt'.

    The run declares its own class as a `[stop] class=...` line -- it caught the
    exception, so it knows. Reading the LAST such line is robust against a blip
    that recovered earlier in the same cycle leaving 'cli not found' prose in the
    tail: that prose must not reroute a real usage stop into a short backoff.
    Only when no class line is present (an older run, or a crash before the line
    was emitted) does this fall back to scanning for usage-limit substrings.
    """
    matches = _STOP_RE.findall(tail or "")
    if matches:
        klass = matches[-1].lower()
        return klass if klass in ("transient", "usage", "halt") else "halt"
    low = (tail or "").lower()
    if any(s in low for s in USAGE_SIGNS):
        return "usage"
    return "halt"


def _answer_count(run_dir: pathlib.Path) -> int:
    """Rows checkpointed to answers.jsonl -- the run's forward-progress meter."""
    path = run_dir / "answers.jsonl"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def is_complete(run_dir: pathlib.Path) -> bool:
    """The run has genuinely finished: a report exists, the manifest is not
    quota-stopped, and no answered row is still ungraded."""
    import json

    manifest = run_dir / "manifest.json"
    if not (run_dir / "report.md").exists() or not manifest.exists():
        return False
    try:
        m = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return (not m.get("quota_stop")) and m.get("rows_answered_but_ungraded", 1) == 0


def _log(log_path: pathlib.Path, msg: str) -> None:
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _tail(path: pathlib.Path, n_bytes: int = 40000) -> str:
    """Last chunk of a file (the usage/reset message lands near the end)."""
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            if size > n_bytes:
                fh.seek(size - n_bytes)
            return fh.read()
    except OSError:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(prog="bench.supervise")
    ap.add_argument("--run-dir", required=True, help="the run's checkpoint dir")
    ap.add_argument("--log", required=True, help="supervisor log file")
    ap.add_argument("--max-cycles", type=int, default=12)
    ap.add_argument("--fallback-sleep-hours", type=float, default=4.5)
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- then the run command, e.g. -- python -m bench.core.run ...")
    args = ap.parse_args()

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        raise SystemExit("no run command given after --")
    run_dir = pathlib.Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = pathlib.Path(args.log)

    # Singleton guard BEFORE any work: a second supervisor on the same run would
    # answer the same questions twice and corrupt the checkpoint.
    if not acquire_singleton_lock(run_dir, log_path):
        return 4

    _log(log_path, f"supervisor start; up to {args.max_cycles} cycles; cmd = {' '.join(cmd)}")
    transient_streak = 0
    for cycle in range(1, args.max_cycles + 1):
        answers_before = _answer_count(run_dir)
        _log(log_path, f"cycle {cycle}: launching run/resume (answers={answers_before})")
        cycle_out = run_dir / f"supervisor-cycle-{cycle}.out"
        with cycle_out.open("w", encoding="utf-8") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace", check=False)
        tail = _tail(cycle_out)
        answers_after = _answer_count(run_dir)
        progressed = answers_after > answers_before
        last_lines = "\n    ".join(tail.splitlines()[-6:])
        _log(log_path, f"cycle {cycle} exited rc={proc.returncode}; answers "
                       f"{answers_before}->{answers_after}; tail:\n    {last_lines}")

        if is_complete(run_dir):
            _log(log_path, f"RUN COMPLETE after {cycle} cycle(s). Supervisor exiting.")
            return 0

        klass = classify_stop(tail)

        if klass == "transient":
            # A self-healing CLI-launch outage (a Claude Code self-update swapping
            # claude.exe). Back off briefly and relaunch -- the client already
            # retried in-call, so this is the longer-outage backstop. Forward
            # progress resets the streak: a slow-but-advancing series of blips
            # must not trip the cap and halt a run that is genuinely moving.
            if progressed:
                transient_streak = 0
            transient_streak += 1
            if transient_streak > MAX_TRANSIENT_RETRIES:
                _log(log_path, f"cycle {cycle}: transient CLI-launch failure persisted "
                               f"for {transient_streak} cycles without progress -- treating "
                               "as a hard failure. Halting for manual attention.")
                return 2
            secs = min(TRANSIENT_BACKOFF_CAP_S, 30.0 * (2 ** (transient_streak - 1)))
            _log(log_path, f"transient CLI-launch failure (streak {transient_streak}, "
                           f"progressed={progressed}); backing off {secs:.0f}s then relaunching")
            time.sleep(secs)
            continue

        # A real stop clears the transient streak.
        transient_streak = 0

        if klass == "usage":
            target = parse_reset(tail)
            secs = args.fallback_sleep_hours * 3600
            if target is not None:
                wake = target + datetime.timedelta(minutes=10)
                parsed = (wake - datetime.datetime.now(target.tzinfo)).total_seconds()
                if 0 < parsed <= MAX_PARSED_SLEEP_S:
                    secs = max(60.0, parsed)
                    _log(log_path, f"usage limit hit; reset parsed as {target.isoformat()}; "
                                   f"sleeping {secs/3600:.2f}h until {wake.isoformat()} (+10m buffer)")
                else:
                    _log(log_path, f"usage limit hit; parsed reset {target.isoformat()} is "
                                   f"out of guard range -- fallback sleep {args.fallback_sleep_hours:.1f}h")
            else:
                _log(log_path, f"usage limit hit; reset time UNPARSED; "
                               f"fallback sleep {args.fallback_sleep_hours:.1f}h")
            time.sleep(secs)
            continue

        # Not complete, not a usage limit, not transient -> a genuine failure. Do
        # not loop on it (that would spin); halt for manual attention.
        _log(log_path, f"cycle {cycle}: stopped but NOT complete and NOT a usage "
                       "limit or transient CLI failure -- an unexpected failure. "
                       "Halting for manual attention.")
        return 2

    _log(log_path, f"safety cap of {args.max_cycles} cycles reached without "
                   "completion. Halting; rerun the supervisor to continue.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
