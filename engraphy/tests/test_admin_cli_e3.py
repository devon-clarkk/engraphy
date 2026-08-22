"""CLI wiring for the E3 admin verbs (migrate / verify-restore / doctor /
pack upgrade) -- confirms cli.py plumbs arguments through and translates
domain errors to typer.BadParameter (clean CLI exit, no raw traceback), the
same contract test_admin_cli.py already establishes for the E2 verbs. The
underlying logic is exercised in depth by test_migrate.py, test_verify_restore.py,
test_doctor.py, and test_pack_upgrade.py; these tests are wiring-only.
"""
import pathlib

import psycopg
import yaml
from typer.testing import CliRunner

from conftest import DATABASE_URL
from engraphy.admin.cli import app

runner = CliRunner()
REPO_ROOT = pathlib.Path(__file__).parents[2]

_CLEANUP_TABLES = ("audit_log", "inbox", "pending_writes", "dedup_log", "edges", "nodes",
                   "scope_grants", "scopes", "edge_rules", "edge_types", "node_types",
                   "api_tokens", "config", "principals")


def _cleanup(space_id):
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        cur = c.cursor()
        for table in _CLEANUP_TABLES:
            cur.execute(f"DELETE FROM {table} WHERE space_id = %s", (space_id,))
        cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))


def _run(*args):
    return runner.invoke(app, [*args, "--database-url", DATABASE_URL])


def test_doctor_cmd_reports_on_a_real_space():
    space_id = "cli-e3-doctor"
    _cleanup(space_id)
    try:
        result = _run("space", "create", "--id", space_id, "--display-name", "D",
                       "--principal", "p1")
        assert result.exit_code == 0, result.output
        result = _run("doctor", "--space", space_id)
        assert result.exit_code == 0, result.output
        assert "attrs_nonconforming: 0" in result.output
        assert "stale pendings" in result.output
    finally:
        _cleanup(space_id)


def test_doctor_cmd_unknown_space_is_clean_error():
    result = _run("doctor", "--space", "no-such-space-at-all")
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_pack_upgrade_cmd_applies_additive_change(tmp_path):
    space_id = "cli-e3-upgrade"
    _cleanup(space_id)
    starter = REPO_ROOT / "packs" / "starter" / "pack.yaml"
    try:
        result = _run("space", "create", "--id", space_id, "--display-name", "U",
                       "--principal", "p1")
        assert result.exit_code == 0, result.output
        result = _run("pack", "apply", str(starter), "--space", space_id)
        assert result.exit_code == 0, result.output

        pack = yaml.safe_load(starter.read_text(encoding="utf-8"))
        pack["version"] += 1
        pack["node_types"]["widget"] = {"description": "A brand new type.", "attrs": {"closed": False}}
        upgraded = tmp_path / "cli_upgrade_test_pack.yaml"
        upgraded.write_text(yaml.safe_dump(pack), encoding="utf-8")
        result = _run("pack", "upgrade", str(upgraded), "--space", space_id)
        assert result.exit_code == 0, result.output
        assert "additive: node type 'widget' added" in result.output
    finally:
        _cleanup(space_id)


def test_pack_upgrade_cmd_destructive_refusal_prints_worklist_and_exits_nonzero(tmp_path):
    space_id = "cli-e3-upgrade-refuse"
    _cleanup(space_id)
    starter = REPO_ROOT / "packs" / "starter" / "pack.yaml"
    try:
        result = _run("space", "create", "--id", space_id, "--display-name", "U",
                       "--principal", "p1")
        assert result.exit_code == 0, result.output
        result = _run("pack", "apply", str(starter), "--space", space_id)
        assert result.exit_code == 0, result.output

        pack = yaml.safe_load(starter.read_text(encoding="utf-8"))
        pack["version"] += 1
        removed_type = next(iter(pack["node_types"]))
        insert_node_of_that_type(space_id, removed_type)
        del pack["node_types"][removed_type]
        pack["edge_rules"] = [r for r in pack["edge_rules"]
                                if r["src"] not in (removed_type, "*") and r["dst"] not in (removed_type, "*")]
        # briefing sections may reference the removed type -- irrelevant to
        # this test (which is about upgrade()'s destructive-refusal path,
        # not briefing/pack validity), so drop it to keep pack-validate clean.
        pack["briefing"] = {"sections": []}
        removed = tmp_path / "cli_upgrade_test_pack_removed.yaml"
        removed.write_text(yaml.safe_dump(pack), encoding="utf-8")
        result = _run("pack", "upgrade", str(removed), "--space", space_id)
        assert result.exit_code != 0
        assert "active node" in result.output
    finally:
        _cleanup(space_id)


def insert_node_of_that_type(space_id, node_type):
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        cur = c.cursor()
        cur.execute(
            "SELECT id FROM scopes WHERE space_id = %s AND owner_principal = 'p1' LIMIT 1", (space_id,))
        scope_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO nodes (space_id, type, scope_id, title, body, attrs, embedding, "
            "embedding_model, source_client, author_principal) VALUES "
            "(%s, %s, %s, 'A test node title', 'A test node body.', '{}', %s, 'test', 'pytest', 'p1')",
            (space_id, node_type, scope_id, "[" + ",".join(["0"] * 384) + "]"),
        )


def test_verify_restore_cmd_missing_dump_file_is_clean_error(tmp_path):
    result = _run("verify-restore", "--against", str(tmp_path / "does-not-exist.pgdump"))
    assert result.exit_code != 0
    assert "not found" in result.output


def test_migrate_cmd_missing_pg_dump_is_clean_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no pg_dump/dbmate reachable
    result = _run("migrate", "--dump-dir", str(tmp_path))
    assert result.exit_code != 0
    assert "pg_dump not found" in result.output
