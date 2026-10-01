"""Every entry point that prints progress encodes UTF-8 before it prints any.

The arrow in an ingest line killed a supervisor twice on 2026-10-02, after nine
ingests had succeeded, because `bench.core.run` reconfigured its streams and the
supervisor wrapping it did not. One helper now, used by all of them, and this
test is what keeps a new entry point from repeating it.
"""

from __future__ import annotations

import io
import pathlib

import pytest

from bench.console import use_utf8_streams

REPO = pathlib.Path(__file__).resolve().parents[2]
ENTRY_POINTS = ["supervise.py", "reference_pass.py", "extraction_coverage.py", "replay.py",
                "offline.py", "k_sweep.py", "retrieval_recall.py"]


@pytest.mark.parametrize("name", ENTRY_POINTS)
def test_the_entry_point_asks_for_utf8_streams(name):
    source = (REPO / "bench" / name).read_text(encoding="utf-8")
    assert "use_utf8_streams()" in source, f"bench/{name} prints progress on locale encoding"


def test_the_run_module_still_reconfigures_its_own_streams():
    """run.py does it inside its win32 block, beside the event loop policy."""
    source = (REPO / "bench" / "core" / "run.py").read_text(encoding="utf-8")
    assert 'reconfigure(encoding="utf-8", errors="replace")' in source


def test_calling_it_twice_is_safe_and_a_plain_stream_does_not_raise(monkeypatch):
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    use_utf8_streams()
    use_utf8_streams()


def test_an_arrow_survives_a_cp1252_stream(monkeypatch):
    """The exact character that crashed the supervisor, through a stream whose
    encoding cannot represent it."""
    buffer = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr("sys.stdout", buffer)
    monkeypatch.setattr("sys.stderr", buffer)
    use_utf8_streams()
    print("conv-26 / llm \u2192 hs-conv-26-llm")
