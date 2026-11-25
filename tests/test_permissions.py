"""Server mode: users, roles per server and project, admin pages, the audit log."""

from app import create_app
from app.db import get_db

from .conftest import PASSWORD, Browser, login, user_id
from .test_app import text


def test_first_user_from_the_command_line_is_an_admin(tmp_path):
    app = create_app({"TESTING": True, "SERVER_MODE": True, "DATA_DIR": str(tmp_path)})
    runner = app.test_cli_runner()
    result = runner.invoke(args=["create-user", "alice"], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert "as an admin" in result.output
    result = runner.invoke(args=["create-user", "bob"], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert "as an admin" not in result.output
    result = runner.invoke(args=["create-user", "carl"], input="short\nshort\n")
    assert result.exit_code != 0


def test_older_databases_keep_their_users_as_admins(tmp_path):
    import sqlite3

    data = tmp_path / "old"
    data.mkdir()
    db = sqlite3.connect(data / "incus-console.db")
    db.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, "
        "password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    db.execute("INSERT INTO users (username, password_hash) VALUES ('old', 'x')")
    db.commit()
    db.close()
    app = create_app({"TESTING": True, "SERVER_MODE": True, "DATA_DIR": str(data)})
    with app.app_context():
        row = get_db().execute("SELECT is_admin, disabled FROM users").fetchone()
    assert tuple(row) == (1, 0)


def test_viewer_sees_but_cannot_change(server_app, setup, fake):
    vic = login(server_app, "vic")
    base = f"/servers/{setup}"
    page = text(vic.get(f"{base}/instances/web-1"))
    assert "web-1" in page
    for hidden in ("Terminal", ">Stop<", "Rename", "Take snapshot", "Edit YAML", "Files"):
        assert hidden not in page
    assert "Create instance" not in text(vic.get(f"{base}/instances"))
    assert vic.post(f"{base}/instances/web-1/action", {"action": "stop"}).status_code == 403
    assert fake.instances["web-1"]["status"] == "Running"
    assert vic.get(f"{base}/instances/web-1/files").status_code == 403
    assert vic.get(f"{base}/instances/web-1/terminal").status_code == 403
    assert vic.get(f"{base}/instances/web-1/edit").status_code == 403
    assert vic.get(f"{base}/profiles/new").status_code == 403


def test_operator_works_with_instances_but_does_not_reconfigure(server_app, setup, fake):
    otto = login(server_app, "otto")
    base = f"/servers/{setup}"
    page = text(otto.get(f"{base}/instances/web-1"))
    assert "Terminal" in page and ">Stop<" in page and "Take snapshot" in page
    assert "Edit YAML" not in page and "Rename" not in page and "Restore" not in page
    otto.post(f"{base}/instances/web-1/action", {"action": "stop", "back": "detail"})
    assert fake.instances["web-1"]["status"] == "Stopped"
    assert otto.get(f"{base}/instances/web-1/files").status_code == 200
    assert otto.post(f"{base}/instances/web-1/delete").status_code == 403
    assert "web-1" in fake.instances


def test_grants_are_per_project(server_app, setup):
    vic = login(server_app, "vic")
    base = f"/servers/{setup}"
    page = text(vic.get(f"{base}/instances"))
    assert '<option value="team-a"' not in page  # the picker only offers granted projects
    # Asking for another project falls back to one the user has.
    page = text(vic.get(f"{base}/instances?project=team-a"))
    assert '<option value="default" selected' in page
    assert vic.post(f"{base}/projects/team-a/use").status_code == 403
    assert ">team-a<" not in text(vic.get(f"{base}/projects"))


def test_all_projects_grant_and_admin_only_project_settings(server_app, setup, fake):
    pam = login(server_app, "pam")
    base = f"/servers/{setup}"
    page = text(pam.get(f"{base}/projects"))
    assert ">team-a<" in page and "Create project" not in page
    assert pam.post(f"{base}/projects/team-a/use").status_code == 302
    assert pam.get(f"{base}/projects/new").status_code == 403
    assert pam.post(f"{base}/projects/team-a/delete").status_code == 403
    assert "team-a" in fake.projects
    detail = text(pam.get(f"{base}/projects/team-a"))
    assert "Configure" not in detail and "Edit YAML" not in detail
    # Inside a project a project admin can do everything.
    assert pam.get(f"{base}/instances/web-1/edit").status_code == 200


def test_servers_are_hidden_without_access(server_app, setup, admin):
    nobody = login(server_app, "vic")
    uid = user_id(server_app, "vic")
    with server_app.app_context():
        get_db().execute("DELETE FROM grants WHERE user_id = ?", (uid,))
        get_db().commit()
    page = text(nobody.get("/servers"))
    assert "Lab" not in page and "do not have access" in page and "Add a server" not in page
    assert nobody.get(f"/servers/{setup}/instances").status_code == 404
    assert nobody.post("/servers", {"kind": "unix", "address": "/x"}).status_code == 403
    assert nobody.post(f"/servers/{setup}/delete").status_code == 403
    assert nobody.get("/admin/users").status_code == 403


def test_one_server_goes_straight_to_it(server_app, setup):
    otto = login(server_app, "otto")
    assert otto.get("/").headers["Location"].endswith(f"/servers/{setup}/instances")


def test_admin_manages_users(server_app, admin):
    page = text(admin.get("/admin/users"))
    assert "root" in page and "(you)" in page
    assert "least 10" in text(
        admin.post("/admin/users/new", {"username": "x", "password": "short", "confirm": "short"})
    )
    done = admin.post(
        "/admin/users/new",
        {"username": "newbie", "password": PASSWORD, "confirm": PASSWORD},
        follow_redirects=True,
    )
    assert "User newbie created" in text(done)
    login(server_app, "newbie")
    uid = user_id(server_app, "newbie")
    new = "another long one"
    admin.post(f"/admin/users/{uid}/password", {"password": new, "confirm": new})
    bad = Browser(server_app).post("/login", {"username": "newbie", "password": PASSWORD})
    assert "Wrong username" in text(bad)
    admin.post(f"/admin/users/{uid}/delete")
    assert ">newbie</a>" not in text(admin.get("/admin/users"))


def test_disabling_signs_the_user_out(server_app, setup, admin):
    otto = login(server_app, "otto")
    uid = user_id(server_app, "otto")
    admin.post(f"/admin/users/{uid}/settings", {"disabled": "1"})
    assert otto.get(f"/servers/{setup}/instances").status_code == 302
    again = Browser(server_app).post("/login", {"username": "otto", "password": PASSWORD})
    assert "Wrong username" in text(again)


def test_the_last_admin_stays(server_app, admin):
    uid = user_id(server_app, "root")
    page = text(admin.post(f"/admin/users/{uid}/settings", {}, follow_redirects=True))
    assert "at least one active admin" in page
    assert "Delete</button>" not in text(admin.get(f"/admin/users/{uid}"))
    assert admin.post(f"/admin/users/{uid}/delete", follow_redirects=True).status_code == 200
    assert "root" in text(admin.get("/admin/users"))


def test_account_page(server_app, setup):
    otto = login(server_app, "otto")
    page = text(otto.get("/account"))
    assert "Lab" in page and "Operator" in page
    wrong = otto.post(
        "/account", {"current": "nope", "password": "new password 1", "confirm": "new password 1"}
    )
    assert "not right" in text(wrong)
    done = otto.post(
        "/account",
        {"current": PASSWORD, "password": "new password 1", "confirm": "new password 1"},
        follow_redirects=True,
    )
    assert "Password changed" in text(done)


def test_audit_log(server_app, setup, admin, fake):
    otto = login(server_app, "otto")
    base = f"/servers/{setup}"
    otto.post(f"{base}/instances/web-1/action", {"action": "stop", "back": "detail"})
    otto.post(f"{base}/instances/web-1/delete")
    Browser(server_app).post("/login", {"username": "otto", "password": "wrong"})
    page = text(admin.get("/admin/audit"))
    assert "instance action: stop" in page
    assert "Not allowed" in page
    assert "Failed log in" in page
    assert "grant add" in page
    assert PASSWORD not in page
    assert otto.get("/admin/audit").status_code == 403
    mine = text(admin.get("/admin/audit?user=otto"))
    assert "grant add" not in mine and "instance action: stop" in mine


def test_desktop_mode_has_no_user_admin(app):
    assert app.test_client().get("/admin/users").status_code == 404
    assert app.test_client().get("/account").status_code == 404


def test_pages_show_who_is_signed_in(server_app, setup, admin):
    page = text(admin.get("/servers"))
    assert "Signed in as" in page and 'whoami__role is-admin">Console admin<' in page
    assert "root (console admin)" in page
    page = text(login(server_app, "vic").get("/servers"))
    assert 'whoami__name">vic<' in page and 'whoami__role">User<' in page
    assert "(console admin)" not in page
