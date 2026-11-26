"""About box and version."""

import json
import re
from pathlib import Path

from app import version
from app.version import __version__, build

from .conftest import Browser, login
from .test_app import text


def test_version_is_ubuntu_style_and_matches_the_desktop_app():
    assert re.fullmatch(r"\d{2}\.(0[1-9]|1[0-2])\.\d+", __version__)
    package = json.loads((Path(__file__).parent.parent / "desktop" / "package.json").read_text())
    assert package["version"] == __version__


def test_build_label(monkeypatch, tmp_path):
    build.cache_clear()
    monkeypatch.setenv("INCUS_CONSOLE_COMMIT", "abc123")
    assert build()["label"] == f"{__version__}-dev"
    assert build()["commit"] == "abc123"
    build.cache_clear()
    monkeypatch.delenv("INCUS_CONSOLE_COMMIT")
    monkeypatch.setattr(version, "HERE", tmp_path)
    (tmp_path / "COMMIT").write_text("def456 release\n")
    assert build()["label"] == __version__ and build()["commit"] == "def456"
    build.cache_clear()


def test_desktop_about(browser, sid):
    page = text(browser.get(f"/about?sid={sid}"))
    assert "<html" in page and "About Incus Console" in page
    assert "Desktop" in page and "Signed in as" not in page
    assert "6.0" in page  # the fake server's Incus version
    assert "Database" in page  # the single desktop user sees installation details
    assert "Apache License 2.0" in page
    assert "Reached through member" not in page
    assert f"Version {build()['label']}" in text(browser.get("/servers"))


def test_partial_for_the_dialog(browser):
    page = text(browser.get("/about", headers={"X-Partial": "1"}))
    assert "<html" not in page and "Copy details" in page and "Close" in page


def test_server_mode_hides_installation_details_from_users(server_app, setup):
    vic = login(server_app, "vic")
    page = text(vic.get(f"/about?sid={setup}"))
    assert "vic (User)" in page and "Incus version" in page
    assert "Database" not in page and "Console certificate" not in page
    admin_page = text(login(server_app, "root").get("/about"))
    assert "root (Console admin)" in admin_page and "Database" in admin_page


def test_about_needs_login_in_server_mode(server_app):
    assert Browser(server_app).get("/about").status_code == 302


def test_about_names_the_cluster_member(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    assert "Reached through member node1" in text(browser.get(f"/about?sid={sid}"))
