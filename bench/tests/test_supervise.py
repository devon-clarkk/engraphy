"""Auto-resume supervisor: reset-time parsing and completion detection.

Hermetic -- no subprocess, no sleep, no Claude. Pins the two decisions that make
the run survive a usage limit: WHEN to wake, and WHETHER it is actually done.
"""

from __future__ import annotations

import datetime
import json

from bench.supervise import _answer_count, classify_stop, is_complete, parse_reset


def test_parse_reset_from_real_message():
    # The exact CLI shape seen live: "resets 5:50pm (Australia/Perth)".
    msg = ('...is_error":true,"api_error_status":429,...'
           '"result":"You\'re out of extra usage · resets 5:50pm (Australia/Perth)"...')
    t = parse_reset(msg)
    assert t is not None
    assert (t.hour, t.minute) == (17, 50)
    assert str(t.tzinfo) == "Australia/Perth"
    # Always in the future.
    assert t > datetime.datetime.now(t.tzinfo)


def test_parse_reset_variants():
    assert parse_reset("resets 9am (America/New_York)").hour == 9
    assert parse_reset("resets 12:30 p.m. (UTC)").hour == 12
    assert parse_reset("resets 12am (UTC)").hour == 0     # midnight
    assert parse_reset("no reset here") is None
    assert parse_reset("resets 5:50pm (Not/AZone)") is None  # bad tz -> fallback


def test_is_complete(tmp_path):
    (tmp_path / "report.md").write_text("x", encoding="utf-8")
    # quota-stopped -> not complete
    (tmp_path / "manifest.json").write_text(
        json.dumps({"quota_stop": True, "rows_answered_but_ungraded": 0}), encoding="utf-8")
    assert is_complete(tmp_path) is False
    # answered-but-ungraded remain -> not complete
    (tmp_path / "manifest.json").write_text(
        json.dumps({"quota_stop": False, "rows_answered_but_ungraded": 40}), encoding="utf-8")
    assert is_complete(tmp_path) is False
    # clean finish -> complete
    (tmp_path / "manifest.json").write_text(
        json.dumps({"quota_stop": False, "rows_answered_but_ungraded": 0}), encoding="utf-8")
    assert is_complete(tmp_path) is True


def test_parse_reset_widened_variants():
    # 24-hour time, no am/pm, no timezone -> machine-local zone, time preserved.
    t = parse_reset("resets 17:50")
    assert t is not None and (t.hour, t.minute) == (17, 50)
    # "reset at ..." phrasing, with a zone.
    assert parse_reset("your limit will reset at 3:00 (UTC)").hour == 3
    # A bare hour with neither minutes nor am/pm is too ambiguous -> None.
    assert parse_reset("resets 5") is None
    # The real monthly-limit variant carries no time at all -> None (fallback).
    assert parse_reset("you've hit your org's monthly usage limit") is None


def test_classify_stop_reads_declared_class_line():
    assert classify_stop("...\n  [stop] class=transient\n  spaces KEPT") == "transient"
    assert classify_stop("  [stop] class=usage") == "usage"
    assert classify_stop("  [stop] class=halt") == "halt"


def test_classify_stop_declared_class_beats_recovered_blip_prose():
    # A blip that RECOVERED left 'cli was not found' prose earlier in the tail;
    # the terminal declared class must still route the real usage stop.
    tail = ("the CLI was not found; retrying...\n"
            "...answered 200/500...\n"
            "  [quota] usage limit reached\n"
            "  [stop] class=usage\n")
    assert classify_stop(tail) == "usage"


def test_classify_stop_last_class_line_wins():
    assert classify_stop("[stop] class=transient\n[stop] class=usage") == "usage"


def test_classify_stop_falls_back_to_usage_signs_without_a_class_line():
    assert classify_stop("...You're out of extra usage . resets 5pm...") == "usage"
    assert classify_stop("some unexpected traceback\nKeyError: 'x'") == "halt"


def test_answer_count(tmp_path):
    assert _answer_count(tmp_path) == 0  # no file yet
    (tmp_path / "answers.jsonl").write_text('{"a":1}\n{"a":2}\n\n', encoding="utf-8")
    assert _answer_count(tmp_path) == 2  # blank lines are not rows


def test_is_complete_missing_files(tmp_path):
    assert is_complete(tmp_path) is False  # no report / manifest yet


def test_an_ingest_only_run_is_complete_without_a_report(tmp_path):
    """Only the report phase writes report.md, so requiring that file made an
    ingest-only pass unfinishable: the supervisor retried a store that was
    already correct, then called it a genuine failure. Seen live 2026-10-01 on
    the seen-split extraction A/B, after all six ingests had succeeded."""
    (tmp_path / "manifest.json").write_text(json.dumps({
        "phases_requested": ["ingest"], "phases_completed": ["ingest"],
        "quota_stop": False}), encoding="utf-8")
    assert is_complete(tmp_path)


def test_a_phase_that_did_not_run_to_the_end_is_not_complete(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "phases_requested": ["ingest", "answer"], "phases_completed": ["ingest"],
        "quota_stop": False}), encoding="utf-8")
    assert not is_complete(tmp_path)


def test_a_judged_run_with_ungraded_rows_is_not_complete(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "phases_requested": ["answer", "judge"], "phases_completed": ["answer", "judge"],
        "quota_stop": False, "rows_answered_but_ungraded": 4}), encoding="utf-8")
    assert not is_complete(tmp_path)


def test_a_quota_stop_is_never_complete(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "phases_requested": ["ingest"], "phases_completed": ["ingest"],
        "quota_stop": True}), encoding="utf-8")
    assert not is_complete(tmp_path)


def test_a_run_from_before_the_phase_bookkeeping_still_uses_the_report_file(tmp_path):
    """The published runs have no phase keys, and they must keep reading as
    complete."""
    (tmp_path / "manifest.json").write_text(json.dumps({
        "quota_stop": False, "rows_answered_but_ungraded": 0}), encoding="utf-8")
    assert not is_complete(tmp_path)
    (tmp_path / "report.md").write_text("ok", encoding="utf-8")
    assert is_complete(tmp_path)
