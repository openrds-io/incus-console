"""Clusters: members, placing and moving instances, evacuate and restore."""

from .conftest import login
from .test_app import text


def test_a_standalone_server_shows_no_cluster(browser, sid, fake):
    fake.add_instance("c1")
    page = text(browser.get(f"/servers/{sid}/instances"))
    assert f"/servers/{sid}/cluster" not in page and "<th>Member</th>" not in page
    assert "is not part of a cluster" in text(browser.get(f"/servers/{sid}/cluster"))
    assert 'name="target"' not in text(browser.get(f"/servers/{sid}/create-instance"))
    assert browser.get(f"/servers/{sid}/instances/c1/move").status_code == 404


def test_members_are_listed(browser, sid, fake):
    fake.add_instance("c1")
    fake.make_cluster("node1", "node2", "node3")
    page = text(browser.get(f"/servers/{sid}/cluster"))
    for name in ("node1", "node2", "node3"):
        assert name in page
    assert "10.0.0.2:8443" in page and "database-leader" in page and "Online" in page
    listing = text(browser.get(f"/servers/{sid}/instances"))
    assert f"/servers/{sid}/cluster" in listing
    assert "<th>Member</th>" in listing and "<td>node1</td>" in listing


def test_create_on_a_chosen_member(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    form = text(browser.get(f"/servers/{sid}/create-instance"))
    assert 'name="target"' in form and 'value="node2"' in form
    data = {"name": "web", "source": "remote", "alias": "debian/13"}
    browser.post(f"/servers/{sid}/create-instance", {**data, "target": "node2"})
    assert fake.instances["web"]["location"] == "node2"
    assert ("POST", "instances", "node2") in fake.targets
    # Left to Incus, no target is sent.
    browser.post(f"/servers/{sid}/create-instance", {**data, "name": "web2"})
    assert fake.instances["web2"]["location"] == "node1"
    assert len(fake.targets) == 1
    refused = browser.post(
        f"/servers/{sid}/create-instance", {**data, "name": "web3", "target": "nowhere"}
    )
    assert "not a cluster member that is online" in text(refused)
    assert "web3" not in fake.instances


def test_move_a_stopped_instance(browser, sid, fake):
    fake.add_instance("c1", status="Stopped")
    fake.add_instance("busy")
    fake.make_cluster("node1", "node2")
    assert "Cluster member" in text(browser.get(f"/servers/{sid}/instances/c1"))
    page = text(browser.get(f"/servers/{sid}/instances/c1/move"))
    assert 'value="node2"' in page and 'value="node1"' not in page
    done = browser.post(
        f"/servers/{sid}/instances/c1/move", {"target": "node2"}, follow_redirects=True
    )
    assert "c1 moved to node2." in text(done)
    assert fake.instances["c1"]["location"] == "node2"
    # A running container cannot move, and neither can one to where it already is.
    assert "Stop busy first" in text(browser.get(f"/servers/{sid}/instances/busy/move"))
    browser.post(f"/servers/{sid}/instances/busy/move", {"target": "node2"})
    assert fake.instances["busy"]["location"] == "node1"
    same = browser.post(f"/servers/{sid}/instances/c1/move", {"target": "node2"})
    assert "Choose a cluster member" in text(same)


def test_evacuate_and_restore(browser, sid, fake):
    fake.add_instance("c1")
    fake.make_cluster("node1", "node2")
    base = f"/servers/{sid}/cluster/members"
    done = browser.post(f"{base}/node1/evacuate", {"mode": "migrate"}, follow_redirects=True)
    assert "node1 evacuated." in text(done) and "Evacuated" in text(done)
    assert fake.instances["c1"]["location"] == "node2"
    # An evacuated member is not offered for new instances.
    assert 'value="node1"' not in text(browser.get(f"/servers/{sid}/create-instance"))
    back = browser.post(f"{base}/node1/restore", follow_redirects=True)
    assert "node1 restored." in text(back)
    assert fake.instances["c1"]["location"] == "node1"
    assert fake.members["node1"]["status"] == "Online"
    assert browser.post(f"{base}/nowhere/evacuate").status_code == 404
    assert "Choose what happens" in text(
        browser.post(f"{base}/node1/evacuate", {"mode": "explode"}, follow_redirects=True)
    )


def test_a_network_is_defined_on_every_member_first(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    done = browser.post(
        f"/servers/{sid}/networks/new",
        {"name": "appnet", "ipv4.address": "auto", "ipv4.nat": "true"},
        follow_redirects=True,
    )
    assert "Network appnet created." in text(done)
    assert [t[2] for t in fake.targets] == ["node1", "node2"]
    assert fake.networks["appnet"]["status"] == "Created"


def test_a_volume_goes_to_the_chosen_member(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    assert 'name="target"' in text(browser.get(f"/servers/{sid}/volumes/new"))
    browser.post(
        f"/servers/{sid}/volumes/new", {"name": "data", "pool": "default", "target": "node2"}
    )
    assert fake.volumes["default"]["data"]["location"] == "node2"
    assert "<td>node2</td>" in text(browser.get(f"/servers/{sid}/volumes"))


def test_only_console_admins_evacuate(server_app, setup, admin, fake):
    fake.make_cluster("node1", "node2")
    for username in ("vic", "otto", "pam"):
        user = login(server_app, username)
        page = text(user.get(f"/servers/{setup}/cluster"))
        assert "node2" in page and "Evacuate" not in page and "Restore" not in page
        refused = user.post(f"/servers/{setup}/cluster/members/node1/evacuate", {"mode": ""})
        assert refused.status_code == 403
    assert fake.members["node1"]["status"] == "Online"
    assert "Evacuate" in text(admin.get(f"/servers/{setup}/cluster"))
