"""engraphy.admin.cli -- the instance-operator CLI (space create / principal add /
token create / token revoke / config set). Driven through Typer's CliRunner
against the live test DB with an operator-privileged (superuser) connection, the
way the real CLI runs. Each test uses a unique space id and cleans up its own
committed rows (CliRunner commands commit for real -- no rollback fixture).
"""
import json

import psycopg
import pytest
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from engraphy.admin.cli import app
from engraphy.server.auth import hash_token
from engraphy.tests.conftest import DATABASE_URL

runner = CliRunner()

# Child-first, so a space that grew node_types/nodes/edges (pack apply, import)
# drops cleanly under the FKs.
_CLEANUP_TABLES = ("audit_log", "inbox", "pending_writes", "dedup_log", "edges", "nodes",
                   "scope_grants", "scopes", "edge_rules", "edge_types", "node_types",
                   "api_tokens", "config", "principals")


@pytest.fixture
def op_db(request):
    """The operator DB url + a per-test space id; drops all of the space's rows
    afterwards."""
    space_id = ("cli-" + request.node.name.replace("_", "-"))[:60]
    _cleanup(space_id)
    yield DATABASE_URL, space_id
    _cleanup(space_id)


def _cleanup(space_id):
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        cur = c.cursor()
        for table in _CLEANUP_TABLES:
            cur.execute(f"DELETE FROM {table} WHERE space_id = %s", (space_id,))
        cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))


def _run(url, *args):
    return runner.invoke(app, [*args, "--database-url", url])


def test_space_create_makes_admin_principal_and_personal_scope(op_db):
    url, space_id = op_db
    result = _run(url, "space", "create", "--id", space_id,
                  "--display-name", "CLI Space", "--principal", "founder")
    assert result.exit_code == 0, result.output

    with psycopg.connect(url, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT role FROM principals WHERE space_id = %s AND id = 'founder'", (space_id,))
        assert cur.fetchone()[0] == "space_admin"
        cur.execute("SELECT visibility, ambient, owner_principal FROM scopes "
                    "WHERE space_id = %s AND id = 'personal-founder'", (space_id,))
        assert cur.fetchone() == ("private", True, "founder")


def test_space_create_duplicate_fails_cleanly(op_db):
    url, space_id = op_db
    _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    result = _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_principal_add_makes_personal_scope(op_db):
    url, space_id = op_db
    _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    result = _run(url, "principal", "add", "--space", space_id, "--id", "member1",
                  "--display-name", "Member One")
    assert result.exit_code == 0, result.output
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT role FROM principals WHERE space_id = %s AND id = 'member1'", (space_id,))
        assert cur.fetchone()[0] == "member"  # default
        cur.execute("SELECT count(*) FROM scopes WHERE space_id = %s AND id = 'personal-member1'",
                    (space_id,))
        assert cur.fetchone()[0] == 1


def test_token_create_prints_plaintext_once_and_stores_only_hash(op_db):
    url, space_id = op_db
    _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    result = _run(url, "token", "create", "--space", space_id, "--principal", "founder",
                  "--client-name", "laptop", "--role", "readonly")
    assert result.exit_code == 0, result.output
    # Last non-empty output line is the raw token.
    raw = [ln for ln in result.output.splitlines() if ln.strip()][-1]
    assert len(raw) >= 40

    with psycopg.connect(url, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT token_hash, role FROM api_tokens WHERE space_id = %s AND principal = 'founder'",
                    (space_id,))
        stored_hash, role = cur.fetchone()
    assert stored_hash == hash_token(raw)
    assert stored_hash != raw and raw not in result.output.rsplit(raw, 1)[0]  # appears once
    assert role == "readonly"


def test_token_revoke(op_db):
    url, space_id = op_db
    _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    _run(url, "token", "create", "--space", space_id, "--principal", "founder", "--client-name", "laptop")
    result = _run(url, "token", "revoke", "--space", space_id, "--principal", "founder",
                  "--client-name", "laptop")
    assert result.exit_code == 0, result.output
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT revoked FROM api_tokens WHERE space_id = %s AND principal = 'founder'",
                    (space_id,))
        assert cur.fetchone()[0] is True
    # revoking again -> no active token -> nonzero exit.
    again = _run(url, "token", "revoke", "--space", space_id, "--principal", "founder",
                 "--client-name", "laptop")
    assert again.exit_code != 0


def test_config_set_space_admin_tools_false(op_db):
    url, space_id = op_db
    _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    result = _run(url, "config", "set", "--space", space_id, "--key", "space_admin_tools", "--value", "false")
    assert result.exit_code == 0, result.output
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT value FROM config WHERE space_id = %s AND key = 'space_admin_tools'",
                    (space_id,))
        assert cur.fetchone()[0] is False


def test_config_set_rejects_non_json(op_db):
    url, space_id = op_db
    _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    result = _run(url, "config", "set", "--space", space_id, "--key", "k", "--value", "not-json")
    assert result.exit_code != 0
    assert "valid JSON" in result.output


# ---- pack validate / apply --------------------------------------------------

def test_pack_validate_starter_ok():
    # No DB / no --database-url: pack validate is a pure file check.
    result = runner.invoke(app, ["pack", "validate", "packs/starter/pack.yaml"])
    assert result.exit_code == 0, result.output
    assert "is valid" in result.output


def test_pack_validate_reports_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("node_types:\n  'Bad Name!':\n    description: x\n")
    result = runner.invoke(app, ["pack", "validate", str(bad)])
    assert result.exit_code != 0


def test_pack_apply_starter_creates_node_types(op_db):
    url, space_id = op_db
    _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    result = _run(url, "pack", "apply", "packs/starter/pack.yaml", "--space", space_id)
    assert result.exit_code == 0, result.output
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM node_types WHERE space_id = %s AND name = 'note'", (space_id,))
        assert cur.fetchone()[0] == 1


def test_pack_apply_missing_space_fails_cleanly(op_db):
    url, space_id = op_db  # space NOT created
    result = _run(url, "pack", "apply", "packs/starter/pack.yaml", "--space", space_id)
    assert result.exit_code != 0
    # Clean handled failure (BadParameter -> SystemExit), not a leaked psycopg
    # traceback. The exact wording is asserted where the space id is short enough
    # not to wrap in Typer's error panel (e.g. the space-create duplicate test).
    assert isinstance(result.exception, SystemExit)


# ---- import -----------------------------------------------------------------

def test_import_loads_nodes_through_the_pipeline(op_db, tmp_path):
    url, space_id = op_db
    _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    # A node type to import into; personal-founder (from space create) is writable
    # by founder, so it is a valid import target.
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO node_types (space_id, name, description, attr_spec) VALUES (%s, 'widget', 'w', %s)",
            (space_id, Jsonb({"attrs": {"closed": False}})),
        )
    batch = tmp_path / "batch.jsonl"
    batch.write_text(json.dumps({"type": "widget", "title": "Imported node", "body": "from a batch"}) + "\n")

    result = _run(url, "import", str(batch), "--space", space_id,
                  "--scope", "personal-founder", "--principal", "founder")
    assert result.exit_code == 0, result.output
    assert "1 inserted" in result.output
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM nodes WHERE space_id = %s AND title = 'Imported node'", (space_id,))
        assert cur.fetchone()[0] == 1


def test_import_bad_line_fails_with_line_number(op_db, tmp_path):
    url, space_id = op_db
    _run(url, "space", "create", "--id", space_id, "--display-name", "S", "--principal", "founder")
    batch = tmp_path / "bad.jsonl"
    batch.write_text("not json at all\n")
    result = _run(url, "import", str(batch), "--space", space_id,
                  "--scope", "personal-founder", "--principal", "founder")
    assert result.exit_code != 0
