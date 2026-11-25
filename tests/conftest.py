import re
import shutil
import tempfile

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db

from .fake_incus import FakeIncus, serve_unix


@pytest.fixture
def short_dir():
    # Unix socket paths are limited to about 100 characters; pytest's tmp_path can exceed that.
    path = tempfile.mkdtemp(prefix="ic-")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def fake():
    return FakeIncus()


@pytest.fixture
def socket_path(fake, short_dir):
    path = f"{short_dir}/incus.sock"
    server = serve_unix(fake, path)
    yield path
    server.shutdown()
    server.server_close()


@pytest.fixture
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "SERVER_MODE": False,
            "DESKTOP_KEY": None,
            "DATA_DIR": str(tmp_path / "data"),
            "LOCAL_SOCKETS": [],
            "ACTION_WAIT": 1,
            "IMAGE_CATALOG": False,
            "INCUS_TIMEOUT": 5,
        }
    )


class Browser:
    """A test client that fills in the CSRF token like a browser form would."""

    def __init__(self, app):
        self.client = app.test_client()

    def get(self, *args, **kwargs):
        return self.client.get(*args, **kwargs)

    def token(self):
        with self.client.session_transaction() as session:
            if "csrf" not in session:
                session["csrf"] = "test-token"
            return session["csrf"]

    def post(self, url, data=None, **kwargs):
        data = {"csrf_token": self.token(), **(data or {})}
        return self.client.post(url, data=data, **kwargs)


@pytest.fixture
def browser(app):
    return Browser(app)


@pytest.fixture
def sid(browser, socket_path):
    """A registered server backed by the fake Incus; returns its id."""
    response = browser.post("/servers", {"kind": "unix", "name": "Local", "address": socket_path})
    assert response.status_code == 302, response.get_data(as_text=True)
    return int(re.search(r"/servers/(\d+)/", response.headers["Location"]).group(1))


# -- server mode: users and roles ------------------------------------------

PASSWORD = "long enough pw"


@pytest.fixture
def server_app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SERVER_MODE": True,
            "DATA_DIR": str(tmp_path / "data"),
            "LOCAL_SOCKETS": [],
            "ACTION_WAIT": 1,
            "IMAGE_CATALOG": False,
            "INCUS_TIMEOUT": 5,
        }
    )
    with app.app_context():
        db = get_db()
        for name, admin in (("root", 1), ("vic", 0), ("otto", 0), ("pam", 0)):
            db.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                (name, generate_password_hash(PASSWORD), admin),
            )
        db.commit()
    return app


def login(app, username):
    browser = Browser(app)
    response = browser.post("/login", {"username": username, "password": PASSWORD})
    assert response.status_code == 302, response.get_data(as_text=True)
    return browser


def user_id(app, username):
    with app.app_context():
        row = get_db().execute("SELECT id FROM users WHERE username = ?", (username,))
        return row.fetchone()[0]


@pytest.fixture
def admin(server_app):
    return login(server_app, "root")


@pytest.fixture
def setup(server_app, admin, socket_path, fake):
    """One server, two projects, an instance in each, and a role for vic, otto and pam."""
    response = admin.post("/servers", {"kind": "unix", "name": "Lab", "address": socket_path})
    sid = int(re.search(r"/servers/(\d+)/", response.headers["Location"]).group(1))
    fake.add_instance("web-1")
    fake.projects["team-a"] = {"name": "team-a", "description": "", "config": {}}
    for username, project, role in (
        ("vic", "default", "viewer"),
        ("otto", "default", "operator"),
        ("pam", "", "project_admin"),  # all projects
    ):
        uid = user_id(server_app, username)
        done = admin.post(
            f"/admin/users/{uid}/grants",
            {"server_id": sid, "project": project, "role": role},
            follow_redirects=True,
        )
        assert "is now" in done.get_data(as_text=True)
    return sid
