"""UTF-8 stdout and stderr, for every entry point that prints progress.

A Windows console defaults to cp1252, and a redirected stream picks up the same
locale encoding, so one non-ASCII character in a progress line raises
`UnicodeEncodeError`. Progress output is never worth a crash: it is the cheapest
part of a run and the arrow in `conv-26 / llm -> hs-conv-26-llm` has no bearing
on the measurement.

Seen live 2026-10-02: `bench.core.run` reconfigured its own streams, and the
supervisor that wraps it did not. Nine ingests of the seen split succeeded, and
the supervisor then died logging the run's own tail back out, which contained an
arrow. The stores were intact both times and the exit code said failure.
"""
from __future__ import annotations

import sys


def use_utf8_streams() -> None:
    """Make stdout and stderr encode UTF-8, replacing anything they cannot.

    Safe to call more than once, and on streams that cannot be reconfigured,
    such as an already-wrapped or closed one.
    """
    for stream in (sys.stdout, sys.stderr):
        try:  # noqa: SIM105 -- the except clause carries the coverage pragma
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover -- redirected stream
            pass
