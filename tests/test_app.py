import os

from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db


def text(response):
    return response.get_data(as_text=True)


# -- access control ----------------------------------------------------------


def server_app(tmp_path):
    app = create_app({"TESTING": True, "SERVER_MODE": True, "DATA_DIR": str(tmp_path / "d")})
    with app.app_context():
        get_db().execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("ada", generate_password_hash("correct horse")),
        )
        get_db().commit()
    return app


def test_server_mode_requires_login(tmp_path):
    client = server_app(tmp_path).test_client()
    response = client.get("/servers")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?next=")


def test_login_and_logout(tmp_path):
    client = server_app(tmp_path).test_client()
    client.get("/login")
    with client.session_transaction() as session:
        token = session["csrf"]
    bad = client.post("/login", data={"csrf_token": token, "username": "ada", "password": "x"})
    assert "Wrong username or password" in text(bad)
    good = client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "ada",
            "password": "correct horse",
            "next": "//evil",
        },
    )
    assert good.headers["Location"] == "/"  # the unsafe next target is ignored
    assert client.get("/servers").status_code == 200
    with client.session_transaction() as session:
        token = session["csrf"]
    client.post("/logout", data={"csrf_token": token})
    assert client.get("/servers").status_code == 302


def test_posts_need_csrf_token(app):
    response = app.test_client().post("/servers", data={"kind": "unix", "address": "/x"})
    assert response.status_code == 400


def test_desktop_key_is_required(tmp_path):
    app = create_app(
        {"TESTING": True, "SERVER_MODE": False, "DESKTOP_KEY": "k3y", "DATA_DIR": str(tmp_path)}
    )
    client = app.test_client()
    assert client.get("/servers").status_code == 401
    assert client.get("/servers?key=wrong").status_code == 401
    assert client.get("/servers?key=k3y").status_code == 200
    assert client.get("/servers").status_code == 200  # the session remembers it


def test_security_headers(browser):
    response = browser.get("/servers")
    policy = response.headers["Content-Security-Policy"]
    assert policy.startswith("default-src 'self'; ")
    assert "connect-src 'self' ws://localhost wss://localhost" in policy
    assert response.headers["X-Frame-Options"] == "DENY"


# -- servers -----------------------------------------------------------------


def test_add_and_remove_server(browser, sid):
    assert "Local" in text(browser.get("/servers"))
    assert browser.get("/").headers["Location"] == f"/servers/{sid}/instances"
    browser.post(f"/servers/{sid}/delete")
    assert "No servers yet" in text(browser.get("/servers"))
    assert browser.get(f"/servers/{sid}/instances").status_code == 404


def test_unreachable_socket_is_not_added(browser, short_dir):
    response = browser.post(
        "/servers", {"kind": "unix", "address": f"{short_dir}/none.sock"}, follow_redirects=True
    )
    assert "Cannot reach Incus" in text(response)
    assert "No servers yet" in text(response)


def test_server_going_away_shows_error_page(browser, sid, socket_path):
    os.unlink(socket_path)
    response = browser.get(f"/servers/{sid}/instances")
    assert response.status_code == 502
    assert "Cannot reach the server" in text(response)


# -- instances ---------------------------------------------------------------


def test_instance_list(browser, sid, fake):
    fake.add_instance("web-1")
    fake.add_instance("idle", status="Stopped")
    page = text(browser.get(f"/servers/{sid}/instances"))
    assert "web-1" in page and "10.0.3." in page
    assert 'value="start"' in page and 'value="stop"' in page


def test_create_container(browser, sid, fake):
    form = text(browser.get(f"/servers/{sid}/create-instance"))
    assert "debian/13" in form and 'value="big"' in form
    response = browser.post(
        f"/servers/{sid}/create-instance",
        {"name": "c1", "source": "remote", "alias": "debian/13", "cpu": "2", "start": "1"},
        follow_redirects=True,
    )
    assert "Container c1 created." in text(response)
    assert fake.instances["c1"]["status"] == "Running"
    assert fake.instances["c1"]["config"]["limits.cpu"] == "2"
    assert fake.instances["c1"]["type"] == "container"


def test_create_error_keeps_the_form(browser, sid, fake):
    form = {"name": "9bad", "source": "remote", "alias": "x"}
    page = text(browser.post(f"/servers/{sid}/create-instance", form))
    assert "Use letters, digits and dashes" in page
    assert 'value="9bad"' in page
    form = {"name": "taken", "source": "remote", "alias": "x"}
    fake.add_instance("taken")
    assert "already exists" in text(browser.post(f"/servers/{sid}/create-instance", form))


def test_instance_actions(browser, sid, fake):
    fake.add_instance("web-1")
    url = f"/servers/{sid}/instances/web-1"
    assert "web-1 stopped." in text(
        browser.post(f"{url}/action", {"action": "stop"}, follow_redirects=True)
    )
    assert fake.instances["web-1"]["status"] == "Stopped"
    browser.post(f"{url}/action", {"action": "start"})
    failed = browser.post(f"{url}/action", {"action": "start"}, follow_redirects=True)
    assert "already running" in text(failed)
    assert "Unknown action" in text(
        browser.post(f"{url}/action", {"action": "explode"}, follow_redirects=True)
    )


def test_slow_operation_hands_off_to_operations(browser, sid, fake):
    fake.add_instance("web-1")
    fake.pending_ops = True
    response = browser.post(
        f"/servers/{sid}/instances/web-1/action", {"action": "stop"}, follow_redirects=True
    )
    assert "Still running" in text(response)
    page = text(browser.get(f"/servers/{sid}/operations"))
    assert "instance web-1" in page
    assert "Cancel" in page


def test_instance_detail_and_delete(browser, sid, fake):
    fake.add_instance("web-1", status="Stopped")
    page = text(browser.get(f"/servers/{sid}/instances/web-1"))
    assert "System container" in page and "limits.cpu" in page and "incusbr0" in page
    assert "volatile.eth0.hwaddr" not in page
    response = browser.post(f"/servers/{sid}/instances/web-1/delete", follow_redirects=True)
    assert "web-1 deleted." in text(response)
    assert "web-1" not in fake.instances


def test_missing_instance_is_404(browser, sid):
    assert browser.get(f"/servers/{sid}/instances/ghost").status_code == 404


def test_snapshots(browser, sid, fake):
    fake.add_instance("web-1")
    url = f"/servers/{sid}/instances/web-1/snapshots"
    assert "Snapshot before created." in text(
        browser.post(url, {"snapshot": "before"}, follow_redirects=True)
    )
    restored = browser.post(f"{url}/before/restore", follow_redirects=True)
    assert "restored to before" in text(restored)
    browser.post(f"{url}/before/delete")
    assert fake.instances["web-1"]["snapshots"] == []


def test_yaml_edit(browser, sid, fake):
    fake.add_instance("web-1")
    url = f"/servers/{sid}/instances/web-1/edit"
    assert "volatile.eth0.hwaddr" in text(browser.get(url))
    bad = browser.post(url, {"yaml": "config: [unclosed"})
    assert "Invalid YAML" in text(bad)
    new = "description: web tier\nconfig:\n  limits.memory: 1GiB\nstatus: ignored\n"
    assert "web-1 updated." in text(browser.post(url, {"yaml": new}, follow_redirects=True))
    assert fake.instances["web-1"]["description"] == "web tier"
    assert fake.instances["web-1"]["config"] == {"limits.memory": "1GiB"}
    assert fake.instances["web-1"]["status"] == "Running"


def test_desktop_key_from_an_earlier_launch_is_refused(tmp_path):
    def launch(key):
        return create_app(
            {"TESTING": True, "SERVER_MODE": False, "DESKTOP_KEY": key, "DATA_DIR": str(tmp_path)}
        )

    first = launch("first-key").test_client()
    assert first.get("/servers?key=first-key").status_code == 200
    with first.session_transaction() as session:
        cookie = dict(session)
    second = launch("second-key").test_client()
    with second.session_transaction() as session:
        session.update(cookie)
    assert second.get("/servers").status_code == 401
