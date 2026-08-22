"""engraphy.core.visibility.ambient_scope_set — design/06: "search(scope=X) ->
X ∪ P-readable ambient scopes" / "dedup candidates ... in X ∪ P-readable
ambient scopes". Exercised against the real DB under the non-superuser app
role so RLS actually gates the ambient-readability half.

Needs cross-connection visibility (bootstrap via superuser `conn`, query via
the non-superuser `app_conn` -- RLS is a no-op against a true superuser
regardless of FORCE), so bootstrap data is committed and explicitly cleaned
up per test rather than relying on conn's rollback-scoped fixture teardown.
"""
import pytest

from conftest import set_identity

from engraphy.core.visibility import ambient_scope_set


def _bootstrap_ambient(conn, space_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO spaces (id, display_name) VALUES (%s, %s)", (space_id, "Ambient Space"))
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, %s, %s), (%s, %s, %s)",
        (space_id, "owner", "Owner", space_id, "member", "Member"),
    )
    # owner's ambient private scope -- auto-joins owner's own queries only.
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, ambient) "
        "VALUES (%s, 'life', 'Life', 'owner', 'private', true)",
        (space_id,),
    )
    # a team-read ambient scope -- auto-joins every member's queries.
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, ambient) "
        "VALUES (%s, 'org', 'Org', 'owner', 'team-read', true)",
        (space_id,),
    )
    # a non-ambient scope the member explicitly targets.
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, ambient) "
        "VALUES (%s, 'project-x', 'Project X', 'member', 'private', false)",
        (space_id,),
    )
    conn.commit()


def _cleanup(conn, space_id):
    cur = conn.cursor()
    cur.execute("DELETE FROM scopes WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM principals WHERE space_id = %s", (space_id,))
    cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
    conn.commit()


@pytest.fixture
def ambient_space(conn, request):
    # spaces.id CHECK: ^[a-z0-9][a-z0-9-]{1,62}$ -- no underscores.
    space_id = ("amb-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_ambient(conn, space_id)
    yield space_id
    _cleanup(conn, space_id)


def test_ambient_scope_set_owner_gets_own_ambient_plus_team_read(app_conn, ambient_space):
    set_identity(app_conn, ambient_space, "owner")
    result = ambient_scope_set(app_conn.cursor(), {"project-x"})
    assert result == {"project-x", "life", "org"}


def test_ambient_scope_set_member_excludes_owners_private_ambient(app_conn, ambient_space):
    set_identity(app_conn, ambient_space, "member")
    result = ambient_scope_set(app_conn.cursor(), {"project-x"})
    # 'life' is owner's private ambient scope -- member can't read it, so RLS
    # excludes it even though it's flagged ambient=true.
    assert result == {"project-x", "org"}


def test_ambient_scope_set_no_explicit_scope_still_gets_ambient(app_conn, ambient_space):
    set_identity(app_conn, ambient_space, "member")
    result = ambient_scope_set(app_conn.cursor(), set())
    assert result == {"org"}
