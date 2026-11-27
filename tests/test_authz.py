"""Roles enforced by Incus: the scriptlet the console writes, and turning it on and off."""

from types import SimpleNamespace

import pytest

from app import authz
from app.db import get_db

from .conftest import login, user_id
from .test_app import text
from .test_user_certs import grant, own_certs, remote  # noqa: F401  (remote is a fixture)

NEEDS = ["authorization_scriptlet", "authorization_client_routing"]


def run(source: str, logged: list | None = None):
    """The scriptlet's authorize(), run as Python (Starlark reads the same)."""
    scope = {"log_info": lambda *parts: (logged if logged is not None else []).append(parts)}
    exec(source, scope)  # noqa: S102 - our own generated text
    return scope["authorize"]


def scriptlet(roles: dict, log: bool = False) -> str:
    import json

    return authz.TEMPLATE.replace("__LOG__", str(log)).replace("__ROLES__", json.dumps(roles))


def who(fingerprint, projects=None):
    certificate = SimpleNamespace(Projects=projects) if projects is not None else None
    return SimpleNamespace(Username=fingerprint, Certificate=certificate)


ROLES = {
    "vic": {"default": "viewer"},
    "otto": {"default": "operator"},
    "pam": {"*": "project_admin"},
}


@pytest.mark.parametrize(
    ("user", "thing", "entitlement", "allowed"),
    [
        # Everyone with a role looks around their project and at the server.
        ("vic", "instance:default/web", "can_view", True),
        ("vic", "server:incus", "can_view", True),
        ("vic", "project:default", "can_view", True),
        # A viewer changes nothing, runs nothing.
        ("vic", "instance:default/web", "can_update_state", False),
        ("vic", "instance:default/web", "can_exec", False),
        ("vic", "instance:default/web", "can_edit", False),
        ("vic", "instance:other/web", "can_view", False),
        # An operator runs things but does not reconfigure or create them.
        ("otto", "instance:default/web", "can_update_state", True),
        ("otto", "instance:default/web", "can_exec", True),
        ("otto", "instance:default/web", "can_manage_snapshots", True),
        ("otto", "storage_volume:default/pool/custom/data", "can_manage_snapshots", True),
        ("otto", "instance:default/web", "can_edit", False),
        ("otto", "project:default", "can_create_instances", False),
        ("otto", "profile:default/default", "can_edit", False),
        # A project admin does everything inside, in any project, but not to the project.
        ("pam", "instance:team-a/db", "can_edit", True),
        ("pam", "project:team-a", "can_create_instances", True),
        ("pam", "profile:team-a/web", "can_edit", True),
        ("pam", "project:team-a", "can_edit", False),
        ("pam", "project:team-a", "can_delete", False),
        # Nobody with a role changes the server.
        ("pam", "server:incus", "can_edit", False),
        ("pam", "server:incus", "can_create_projects", False),
        ("pam", "storage_pool:default", "can_edit", False),
        ("pam", "storage_pool:default", "can_view", True),
    ],
)
def test_the_scriptlet_decides_by_role(user, thing, entitlement, allowed):
    assert run(scriptlet(ROLES))(who(user), thing, entitlement) is allowed


def test_other_restricted_certificates_keep_their_projects():
    authorize = run(scriptlet(ROLES))
    backup = who("backup-tool", projects=["team-a"])
    assert authorize(backup, "instance:team-a/db", "can_edit")
    assert authorize(backup, "project:team-a", "can_create_instances")
    assert authorize(backup, "server:incus", "can_view")
    assert not authorize(backup, "instance:default/web", "can_view")
    assert not authorize(backup, "project:team-a", "can_edit")
    assert not authorize(backup, "server:incus", "can_edit")
    # A certificate nothing is known about gets nothing.
    assert not authorize(who("stranger"), "instance:default/web", "can_view")


def test_the_scriptlet_copes_with_how_incus_passes_details():
    """Some Incus versions nest the request details; the fingerprint is found either way."""
    nested = SimpleNamespace(RequestDetails=SimpleNamespace(Username="otto"))
    assert run(scriptlet(ROLES))(nested, "instance:default/web", "can_exec")
    logged = []
    run(scriptlet(ROLES, log=True), logged)(who("vic"), "instance:default/web", "can_exec")
    assert logged and "vic" in logged[0] and logged[0][-1] is False


def test_turning_it_on_writes_the_scriptlet_and_keeps_it_current(remote, fake):  # noqa: F811
    admin = login(remote, "root")
    grant(admin, remote, "default", role="viewer")
    page = text(admin.get("/servers/1/roles"))
    assert "cannot do this" in page  # the server lacks the scriptlet extensions
    assert "cannot do it" in text(
        admin.post("/servers/1/roles", {"enforce": "on"}, follow_redirects=True)
    )
    fake.extensions += NEEDS
    assert "Off." in text(admin.get("/servers/1/roles"))
    done = admin.post("/servers/1/roles", {"enforce": "on"}, follow_redirects=True)
    assert "Incus now enforces the roles" in text(done) and "On." in text(done)
    assert fake.config["authorization.client.tls-restricted"] == "scriptlet"
    (cert,) = own_certs(fake)
    fingerprint = next(f for f, c in fake.certificates.items() if c is cert)
    sent = fake.config["authorization.scriptlet"]
    assert fingerprint in sent and '"viewer"' in sent
    authorize = run(sent)
    assert authorize(who(fingerprint), "instance:default/web-1", "can_view")
    assert not authorize(who(fingerprint), "instance:default/web-1", "can_update_state")
    assert "enforced by Incus" in text(admin.get("/servers"))
    # A change of role reaches Incus without anyone asking.
    grant(admin, remote, "default", role="operator")
    assert run(fake.config["authorization.scriptlet"])(
        who(fingerprint), "instance:default/web-1", "can_update_state"
    )
    # ... and so does another project.
    grant(admin, remote, "team-a", role="project_admin")
    assert '"team-a": "project_admin"' in fake.config["authorization.scriptlet"]
    # Off again: both settings go.
    off = admin.post("/servers/1/roles", {"enforce": "off"}, follow_redirects=True)
    assert "no longer enforces" in text(off)
    assert "authorization.scriptlet" not in fake.config
    assert "authorization.client.tls-restricted" not in fake.config
    grant(admin, remote, "default", role="viewer")
    assert "authorization.scriptlet" not in fake.config


def test_only_console_admins_turn_it_on(remote, fake):  # noqa: F811
    root = login(remote, "root")
    grant(root, remote, "default")
    fake.extensions += NEEDS
    vic = login(remote, "vic")
    assert vic.get("/servers/1/roles").status_code == 403
    assert vic.post("/servers/1/roles", {"enforce": "on"}).status_code == 403
    assert root.get("/servers/9/roles").status_code == 404
    assert "authorization.scriptlet" not in fake.config
    assert "Roles" not in text(vic.get("/servers"))


def test_it_does_not_apply_to_local_socket_servers(server_app, setup, admin, fake):
    """No per-user certificates there, so nothing to route to a scriptlet."""
    fake.extensions += NEEDS
    assert "Not available here" in text(admin.get(f"/servers/{setup}/roles"))
    admin.post(f"/servers/{setup}/roles", {"enforce": "on"})
    assert "authorization.scriptlet" not in fake.config
    assert user_id(server_app, "vic")


def test_older_databases_gain_the_roles_columns(tmp_path):
    import sqlite3

    from app import create_app
    from app.db import SCHEMA, path_for

    data = tmp_path / "data"
    data.mkdir()
    old = sqlite3.connect(path_for(str(data)))
    old.executescript(
        SCHEMA.replace(
            ",\n    -- Roles enforced by Incus (app/authz.py): on or off, and what was last sent.\n"
            "    enforce_roles INTEGER NOT NULL DEFAULT 0,\n    scriptlet_hash TEXT",
            "",
        )
    )
    old.execute("INSERT INTO servers (name, kind, address) VALUES ('Lab', 'unix', '/x')")
    old.commit()
    old.close()
    app = create_app({"TESTING": True, "SERVER_MODE": True, "DATA_DIR": str(data)})
    with app.app_context():
        assert get_db().execute("SELECT enforce_roles FROM servers").fetchone()[0] == 0


INSTANCE_ROLES = {
    "ina": {"default/web": "operator", "team-a/db": "viewer"},
    "mix": {"default": "viewer", "default/web": "operator"},
}


@pytest.mark.parametrize(
    ("user", "thing", "entitlement", "allowed"),
    [
        # Given one instance: it, and no other.
        ("ina", "instance:default/web", "can_view", True),
        ("ina", "instance:default/web", "can_exec", True),
        ("ina", "instance:default/web/snap0", "can_view", True),
        ("ina", "instance:default/web", "can_edit", False),
        ("ina", "instance:default/other", "can_view", False),
        ("ina", "instance:default/webby", "can_view", False),
        ("ina", "instance:team-a/db", "can_view", True),
        ("ina", "instance:team-a/db", "can_update_state", False),
        # Its project is visible as a name; what else is in it is not.
        ("ina", "project:default", "can_view", True),
        ("ina", "project:default", "can_view_operations", True),
        ("ina", "project:default", "can_create_instances", False),
        ("ina", "profile:default/default", "can_view", False),
        ("ina", "storage_volume:default/pool/custom/data", "can_view", False),
        ("ina", "project:other", "can_view", False),
        ("ina", "server:incus", "can_view", True),
        # A viewer of the project who operates one instance in it.
        ("mix", "instance:default/web", "can_update_state", True),
        ("mix", "instance:default/other", "can_update_state", False),
        ("mix", "instance:default/other", "can_view", True),
        ("mix", "profile:default/default", "can_view", True),
    ],
)
def test_the_scriptlet_knows_single_instances(user, thing, entitlement, allowed):
    assert run(scriptlet(INSTANCE_ROLES))(who(user), thing, entitlement) is allowed


def give_instance(admin, app, username, instance, role="operator", project="default", server=1):
    return admin.post(
        f"/admin/users/{user_id(app, username)}/instance-grants",
        {"server_id": server, "project": project, "instance": instance, "role": role},
        follow_redirects=True,
    )


def test_a_user_given_one_instance_sees_only_that(server_app, setup, admin, fake):
    from werkzeug.security import generate_password_hash

    from .conftest import PASSWORD

    with server_app.app_context():
        get_db().execute(
            "INSERT INTO users (username, password_hash) VALUES ('ina', ?)",
            (generate_password_hash(PASSWORD),),
        )
        get_db().commit()
    fake.add_instance("other")
    base = f"/servers/{setup}"
    ina = login(server_app, "ina")
    assert "do not have access to any servers" in text(ina.get("/servers"))
    done = give_instance(admin, server_app, "ina", "web-1")
    assert "ina is now Operator of web-1 in project default." in text(done)
    assert "Single instances" in text(done) and "<td>web-1</td>" in text(done)
    ina = login(server_app, "ina")
    # The list holds their instance alone; its pages and day-to-day actions work.
    listing = text(ina.get(f"{base}/instances"))
    assert "web-1" in listing and "other" not in listing and ">Stop<" in listing
    assert ina.get(f"{base}/instances/web-1").status_code == 200
    assert ina.get(f"{base}/instances/web-1/logs").status_code == 200
    stopped = ina.post(
        f"{base}/instances/web-1/action",
        {"action": "stop", "back": "detail"},
        follow_redirects=True,
    )
    assert "web-1 stopped." in text(stopped)
    assert ina.post(f"{base}/instances/web-1/snapshots", {"snapshot": "s1"}).status_code == 302
    # Not its neighbour, not the rest of the project, and no reconfiguring.
    assert ina.get(f"{base}/instances/other").status_code == 403
    assert ina.post(f"{base}/instances/other/action", {"action": "stop"}).status_code == 403
    for page in ("profiles", "images", "networks", "volumes", "create-instance", "operations"):
        assert ina.get(f"{base}/{page}").status_code == 403, page
    assert ina.get(f"{base}/instances/web-1/settings").status_code == 403
    assert ina.post(f"{base}/instances/web-1/delete").status_code == 403
    assert ina.get(f"{base}/instances/web-1/copy").status_code == 403
    assert ">Delete<" not in text(ina.get(f"{base}/instances/web-1"))
    # A viewer of one instance can look, and that is all.
    give_instance(admin, server_app, "ina", "web-1", role="viewer")
    ina = login(server_app, "ina")
    assert ina.get(f"{base}/instances/web-1").status_code == 200
    assert ina.post(f"{base}/instances/web-1/action", {"action": "start"}).status_code == 403
    assert ina.get(f"{base}/instances/web-1/terminal").status_code == 403
    # The instance's Access page names them.
    assert "this instance" in text(admin.get(f"{base}/instances/web-1/access"))
    assert "ina" not in text(admin.get(f"{base}/instances/other/access"))
    # Bad input, and only the two roles.
    assert "Name the instance" in text(give_instance(admin, server_app, "ina", "bad name"))
    assert "Pick a server and a role" in text(
        give_instance(admin, server_app, "ina", "web-1", role="project_admin")
    )


def test_single_instance_roles_follow_renames_and_deletions(server_app, setup, admin, fake):
    give_instance(admin, server_app, "vic", "web-1")
    base = f"/servers/{setup}/instances"
    fake.instances["web-1"].update(status="Stopped", status_code=102)
    admin.post(f"{base}/web-1/rename", {"new_name": "web-9"})
    with server_app.app_context():
        assert [r["instance"] for r in get_db().execute("SELECT * FROM instance_grants")] == [
            "web-9"
        ]
    admin.post(f"{base}/web-9/delete")
    with server_app.app_context():
        assert not get_db().execute("SELECT 1 FROM instance_grants").fetchone()
    # Removing one by hand.
    give_instance(admin, server_app, "vic", "db-1")
    with server_app.app_context():
        grant_id = get_db().execute("SELECT id FROM instance_grants").fetchone()[0]
    uid = user_id(server_app, "vic")
    gone = admin.post(
        f"/admin/users/{uid}/instance-grants/{grant_id}/delete", follow_redirects=True
    )
    assert "Access to the instance removed." in text(gone)


def test_single_instances_reach_the_certificate_and_the_scriptlet(remote, fake):  # noqa: F811
    admin = login(remote, "root")
    fake.extensions += NEEDS
    admin.post("/servers/1/roles", {"enforce": "on"})
    give_instance(admin, remote, "vic", "web-1", project="team-a")
    (cert,) = own_certs(fake)
    assert cert["projects"] == ["team-a"]
    assert '"team-a/web-1": "operator"' in fake.config["authorization.scriptlet"]
