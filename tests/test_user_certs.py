"""Per-user restricted certificates on a remote (https) server."""

import pytest
from werkzeug.security import generate_password_hash

from app import create_app, user_certs
from app.certs import ensure_client_cert, fingerprint, generate_self_signed
from app.db import get_db
from app.incus import IncusClient, IncusError

from .fake_incus import serve_tls
from .test_app import text
from .test_permissions import PASSWORD, login, user_id


@pytest.fixture
def remote(tmp_path, fake):
    data = tmp_path / "data"
    data.mkdir()
    console_crt, _ = ensure_client_cert(data)
    server_crt, server_key = tmp_path / "server.crt", tmp_path / "server.key"
    generate_self_signed("fake-incus", server_crt, server_key, server=True)
    server = serve_tls(fake, str(server_crt), str(server_key), client_ca=console_crt)
    fake.add_instance("web-1")
    fake.projects["team-a"] = {"name": "team-a", "description": "", "config": {}}
    app = create_app(
        {
            "TESTING": True,
            "SERVER_MODE": True,
            "DATA_DIR": str(data),
            "LOCAL_SOCKETS": [],
            "ACTION_WAIT": 1,
            "IMAGE_CATALOG": False,
            "INCUS_TIMEOUT": 5,
        }
    )
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO servers (name, kind, address, fingerprint) VALUES (?, 'https', ?, ?)",
            ("Remote", f"https://127.0.0.1:{server.server_address[1]}", fingerprint(server_crt)),
        )
        for name, admin in (("root", 1), ("vic", 0)):
            db.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                (name, generate_password_hash(PASSWORD), admin),
            )
        db.commit()
    yield app
    server.shutdown()
    server.server_close()


def grant(admin, app, project, role="operator", username="vic"):
    uid = user_id(app, username)
    return admin.post(
        f"/admin/users/{uid}/grants",
        {"server_id": 1, "project": project, "role": role},
        follow_redirects=True,
    )


def own_certs(fake):
    return [c for c in fake.certificates.values() if c["name"].startswith("incus-console/")]


def test_grant_adds_a_restricted_certificate(remote, fake):
    admin = login(remote, "root")
    assert "trust store was not updated" not in text(grant(admin, remote, "default"))
    (cert,) = own_certs(fake)
    assert cert["name"] == "incus-console/vic"
    assert cert["restricted"] and cert["projects"] == ["default"]
    vic = login(remote, "vic")
    fake.callers.clear()
    assert "web-1" in text(vic.get("/servers/1/instances"))
    # The page went to Incus with vic's certificate only, not the console's.
    assert set(fake.callers) == {cert["fingerprint"]}


def test_incus_refuses_other_projects(remote, fake):
    admin = login(remote, "root")
    grant(admin, remote, "default")
    with remote.app_context():
        crt, key = user_certs._paths(user_id(remote, "vic"), 1)
        server = get_db().execute("SELECT * FROM servers").fetchone()
    client = IncusClient(
        url=server["address"],
        cert=str(crt),
        key=str(key),
        fingerprint=server["fingerprint"],
        project="team-a",
    )
    with pytest.raises(IncusError) as refused:
        client.instances()
    assert refused.value.status == 403
    assert [p["name"] for p in client.projects()] == ["default"]
    with pytest.raises(IncusError):
        client.update_project("default", {"config": {"limits.instances": "999"}})


def test_removing_access_removes_the_certificate(remote, fake):
    admin = login(remote, "root")
    grant(admin, remote, "default")
    grant(admin, remote, "team-a", "viewer")
    (cert,) = own_certs(fake)
    assert cert["projects"] == ["default", "team-a"]
    uid = user_id(remote, "vic")
    admin.post(f"/admin/users/{uid}/grants/1/delete")
    assert own_certs(fake)[0]["projects"] == ["team-a"]
    admin.post(f"/admin/users/{uid}/grants/2/delete")
    assert own_certs(fake) == []
    with remote.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM user_certs").fetchone()[0] == 0


def test_all_projects_follow_new_projects(remote, fake):
    admin = login(remote, "root")
    grant(admin, remote, "", "project_admin")
    assert own_certs(fake)[0]["projects"] == ["default", "team-a"]
    admin.post("/servers/1/projects/new", {"name": "team-b"})
    assert own_certs(fake)[0]["projects"] == ["default", "team-a", "team-b"]
    # A project made outside the console is picked up when the user asks for it.
    fake.projects["team-c"] = {"name": "team-c", "description": "", "config": {}}
    vic = login(remote, "vic")
    vic.get("/servers/1/instances?project=team-c")
    assert "team-c" in own_certs(fake)[0]["projects"]


def test_renamed_and_deleted_projects(remote, fake):
    admin = login(remote, "root")
    grant(admin, remote, "team-a")
    admin.post("/servers/1/projects/team-a/rename", {"new_name": "team-z"})
    assert own_certs(fake)[0]["projects"] == ["team-z"]
    page = text(admin.get(f"/admin/users/{user_id(remote, 'vic')}"))
    assert "team-z" in page
    admin.post("/servers/1/projects/team-z/delete")
    assert own_certs(fake) == []


def test_admins_and_disabled_users_have_no_certificate(remote, fake):
    admin = login(remote, "root")
    grant(admin, remote, "default")
    uid = user_id(remote, "vic")
    admin.post(f"/admin/users/{uid}/settings", {"is_admin": "1"})
    assert own_certs(fake) == []
    admin.post(f"/admin/users/{uid}/settings", {})
    assert len(own_certs(fake)) == 1
    admin.post(f"/admin/users/{uid}/settings", {"disabled": "1"})
    assert own_certs(fake) == []


def test_deleting_user_or_server_cleans_the_trust_store(remote, fake):
    admin = login(remote, "root")
    grant(admin, remote, "default")
    admin.post(f"/admin/users/{user_id(remote, 'vic')}/delete")
    assert own_certs(fake) == []
    with remote.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO users (username, password_hash) VALUES ('ann', ?)",
            (generate_password_hash(PASSWORD),),
        )
        db.commit()
    grant(admin, remote, "default", username="ann")
    assert len(own_certs(fake)) == 1
    admin.post("/servers/1/delete")
    assert own_certs(fake) == []


def test_unreachable_server_is_reported(remote, fake):
    admin = login(remote, "root")
    with remote.app_context():
        db = get_db()
        db.execute("UPDATE servers SET address = 'https://127.0.0.1:1'")
        db.commit()
    page = text(grant(admin, remote, "default"))
    assert "trust store was not updated" in page
