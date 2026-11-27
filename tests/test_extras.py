"""Audit forwarding, checking for a newer image, access pages, member-specific bridges."""

import logging

from app import audit

from .conftest import login, user_id
from .test_app import text
from .test_groups import join, make_group


def test_audit_line_is_one_line_of_pairs():
    line = audit.line(
        "vic", "Lab", "default", "instance action: stop", "/x", 302, 'web "1"\nstopped.'
    )
    assert line == (
        'user=vic server=Lab project=default action="instance action: stop" '
        'path=/x status=302 outcome="web \\"1\\" stopped."'
    )
    assert audit.line("vic", None, None, "Logged in", "/login", 200, None).endswith(
        'server=- project=- action="Logged in" path=/login status=200 outcome=-'
    )


def test_audit_entries_are_forwarded_when_asked(server_app, setup, fake, caplog):
    otto = login(server_app, "otto")
    url = f"/servers/{setup}/instances/web-1/action"
    # Off by default: nothing leaves the database.
    assert audit._destination is None
    try:
        audit._forward_to("log")
        audit.forwarder.addHandler(caplog.handler)
        with caplog.at_level(logging.INFO, logger="incus_console.audit"):
            otto.post(url, {"action": "stop", "back": "detail"})
        lines = [r.getMessage() for r in caplog.records if r.name == "incus_console.audit"]
        assert any(
            'user=otto server=Lab project=default action="instance action: stop"' in line
            for line in lines
        )
    finally:
        audit.forwarder.removeHandler(caplog.handler)
        audit._forward_to("")
    assert audit._destination is None


def test_a_bad_forward_setting_is_refused(tmp_path):
    import pytest

    from app import create_app

    with pytest.raises(ValueError, match="AUDIT_FORWARD"):
        create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "AUDIT_FORWARD": "carrier pigeon"})
    audit._forward_to("")


def _oci(fake, name="web"):
    fake.add_instance(name, status="Stopped")
    image = fake.add_image("nginx")
    fake.images[image]["update_source"] = {
        "server": "https://docker.io",
        "alias": "nginx:alpine",
        "protocol": "oci",
    }
    fake.instances[name]["config"].update(
        {"volatile.container.oci": "true", "volatile.base_image": image}
    )
    return image


def test_check_for_a_newer_image(browser, sid, fake):
    image = _oci(fake)
    url = f"/servers/{sid}/instances/web/check-image"
    assert "Check for update" in text(browser.get(f"/servers/{sid}/instances/web"))
    same = browser.post(url, follow_redirects=True)
    assert "web is on the newest nginx:alpine." in text(same)
    # A newer build in the registry: the refresh brings it, and the page offers the update.
    fake.refreshes[image] = "nginx (newer)"
    newer = browser.post(url)
    assert newer.headers["Location"].endswith("/instances/web/update-image")
    assert "A newer nginx:alpine is available." in text(browser.get(newer.headers["Location"]))
    # The image has been removed from the server: nothing to compare with.
    del fake.images[image]
    assert "no longer on the server" in text(browser.post(url, follow_redirects=True))
    fake.add_instance("sys")
    refused = browser.post(f"/servers/{sid}/instances/sys/check-image", follow_redirects=True)
    assert "Only application containers" in text(refused)


def test_restart_if_it_exits_is_an_oci_setting(browser, sid, fake):
    _oci(fake)
    url = f"/servers/{sid}/instances/web/settings"
    assert "Restart if it exits" in text(browser.get(url))
    browser.post(url, {"cfg:boot.autorestart": "true"})
    assert fake.instances["web"]["config"]["boot.autorestart"] == "true"
    fake.add_instance("sys")
    assert "Restart if it exits" not in text(browser.get(f"/servers/{sid}/instances/sys/settings"))


def test_access_page_lists_console_users_and_incus(server_app, setup, admin, fake):
    gid = make_group(admin)
    join(admin, server_app, gid, "vic")
    admin.post(
        f"/admin/groups/{gid}/grants", {"server_id": setup, "project": "*", "role": "operator"}
    )
    fake.extensions.append("project_access")
    fake.access = [
        {"identifier": "incus-console/vic", "role": "restricted", "provider": "tls"},
        {"identifier": "ab12cd34", "role": "admin", "provider": "tls"},
    ]
    page = text(admin.get(f"/servers/{setup}/projects/default/access"))
    assert "Console admin" in page and "root" in page
    assert "vic" in page and "ops" in page and "all projects" in page
    assert "incus-console/vic" in page and "ab12cd34" in page
    # Project admins see it for their project; operators and viewers do not.
    pam = login(server_app, "pam")
    assert pam.get(f"/servers/{setup}/projects/default/access").status_code == 200
    assert "Access" in text(pam.get(f"/servers/{setup}/projects/default"))
    for username in ("vic", "otto"):
        assert (
            login(server_app, username).get(f"/servers/{setup}/projects/default/access").status_code
            == 403
        )
    assert admin.get(f"/servers/{setup}/projects/nowhere/access").status_code == 404
    instance = text(admin.get(f"/servers/{setup}/instances/web-1/access"))
    assert "through project default" in instance and "incus-console/vic" in instance
    assert user_id(server_app, "vic")


def test_access_page_on_an_older_incus_and_in_desktop_mode(browser, sid, fake):
    fake.add_instance("c1")
    page = text(browser.get(f"/servers/{sid}/projects/default/access"))
    assert "does not report who has access" in page and "Console users" not in page
    fake.extensions.append("project_access")
    assert "Incus lists nobody" in text(browser.get(f"/servers/{sid}/instances/c1/access"))


def test_a_bridge_takes_external_interfaces_per_member(browser, sid, fake):
    assert "On each member" not in text(browser.get(f"/servers/{sid}/networks/new"))
    fake.make_cluster("node1", "node2")
    form = text(browser.get(f"/servers/{sid}/networks/new"))
    assert 'name="member:node1:bridge.external_interfaces"' in form
    done = browser.post(
        f"/servers/{sid}/networks/new",
        {
            "name": "lan",
            "cfg:ipv4.address": "auto",
            "member:node2:bridge.external_interfaces": "eth1",
        },
        follow_redirects=True,
    )
    assert "Network lan created." in text(done)
    assert fake.network_member_config["lan"] == {
        "node1": {},
        "node2": {"bridge.external_interfaces": "eth1"},
    }
    assert "External interfaces" in text(done) and "<td>eth1</td>" in text(done)
