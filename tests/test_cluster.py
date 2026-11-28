"""Clusters: members, placing and moving instances, evacuate and restore."""

from .conftest import login
from .test_app import text


def test_a_standalone_server_shows_no_cluster(browser, sid, fake):
    fake.add_instance("c1")
    page = text(browser.get(f"/servers/{sid}/instances"))
    assert "<th>Member</th>" not in page
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
    assert "node1 restored." in text(back) and ">Evacuate<" in text(back)
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
    assert [t[2] for t in fake.targets if t[0] == "POST"] == ["node1", "node2"]
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
        assert "node2" in text(user.get(f"/servers/{setup}/cluster"))
        page = text(user.get(f"/servers/{setup}/cluster/members/node1"))
        assert "node1" in page and "Evacuate" not in page and "Restore" not in page
        refused = user.post(f"/servers/{setup}/cluster/members/node1/evacuate", {"mode": ""})
        assert refused.status_code == 403
    assert fake.members["node1"]["status"] == "Online"
    assert "Evacuate" in text(admin.get(f"/servers/{setup}/cluster/members/node1"))
    # The members table itself stays free of buttons.
    assert ">Evacuate<" not in text(admin.get(f"/servers/{setup}/cluster"))


def test_member_page_shows_resources_and_instances(browser, sid, fake):
    fake.add_instance("c1")
    fake.make_cluster("node1", "node2")
    page = text(browser.get(f"/servers/{sid}/cluster/members/node1"))
    assert "2.0 GiB used of 8.0 GiB" in page and "5.0 GiB used of 20.0 GiB" in page
    assert "0.50, 0.25, 0.10" in page and "3 days, 2 hours" in page
    assert f"/servers/{sid}/instances/c1" in page
    assert f"/servers/{sid}/instances/c1" not in text(
        browser.get(f"/servers/{sid}/cluster/members/node2")
    )
    assert browser.get(f"/servers/{sid}/cluster/members/nowhere").status_code == 404


def test_edit_a_member(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    fake.groups["ssd"] = {"name": "ssd", "description": "", "members": [], "config": {}}
    url = f"/servers/{sid}/cluster/members/node1/edit"
    assert "event-hub" in text(browser.get(url))
    form = {"description": "rack 4", "failure_domain": "rack4", "roles": "event-hub"}
    assert "at least one group" in text(browser.post(url, form))
    done = browser.post(url, {**form, "groups": ["default", "ssd"]}, follow_redirects=True)
    assert "node1 updated." in text(done)
    member = fake.members["node1"]
    assert member["description"] == "rack 4" and member["failure_domain"] == "rack4"
    # The role Incus gave is kept next to the one the admin added.
    assert member["roles"] == ["database-leader", "event-hub"]
    assert fake.groups["ssd"]["members"] == ["node1"]


def test_groups(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    base = f"/servers/{sid}/cluster/groups"
    assert "Use letters" in text(browser.post(base, {"name": "bad name"}, follow_redirects=True))
    made = browser.post(base, {"name": "ssd", "description": "Fast disks"}, follow_redirects=True)
    assert "Group ssd created." in text(made)
    saved = browser.post(
        f"{base}/ssd",
        {"name": "fast", "description": "Fast disks", "members": "node2"},
        follow_redirects=True,
    )
    assert "Group fast updated." in text(saved)
    assert fake.groups["fast"]["members"] == ["node2"] and "ssd" not in fake.groups
    assert fake.members["node2"]["groups"] == ["default", "fast"]
    # Instances can go to "any member of" the group.
    form = text(browser.get(f"/servers/{sid}/create-instance"))
    assert 'value="@fast"' in form
    browser.post(
        f"/servers/{sid}/create-instance",
        {"name": "web", "source": "remote", "alias": "debian/13", "target": "@fast"},
    )
    assert fake.instances["web"]["location"] == "node2"
    assert "not a cluster group" in text(
        browser.post(
            f"/servers/{sid}/create-instance",
            {"name": "web2", "source": "remote", "alias": "debian/13", "target": "@nope"},
        )
    )
    # Only empty groups go, and never the default one.
    assert "Only empty" in text(browser.post(f"{base}/fast/delete", follow_redirects=True))
    browser.post(f"{base}/fast", {"name": "fast"})
    assert "Group fast deleted." in text(browser.post(f"{base}/fast/delete", follow_redirects=True))
    assert "cannot be deleted" in text(
        browser.post(f"{base}/default/delete", follow_redirects=True)
    )
    assert "keeps its name" in text(browser.post(f"{base}/default", {"name": "other"}))


def test_only_console_admins_change_members_and_groups(server_app, setup, admin, fake):
    fake.make_cluster("node1", "node2")
    pam = login(server_app, "pam")
    base = f"/servers/{setup}/cluster"
    assert "node1" in text(pam.get(f"{base}/members/node1"))
    assert "Create group" not in text(pam.get(base))
    assert pam.get(f"{base}/members/node1/edit").status_code == 403
    assert pam.post(f"{base}/groups", {"name": "mine"}).status_code == 403
    assert pam.get(f"{base}/groups/default").status_code == 403
    assert pam.post(f"{base}/groups/default/delete").status_code == 403
    assert "mine" not in fake.groups


def test_copy_to_a_chosen_member(browser, sid, fake):
    fake.add_instance("c1", status="Stopped")
    fake.make_cluster("node1", "node2")
    url = f"/servers/{sid}/instances/c1/copy"
    assert 'value="node2"' in text(browser.get(url))
    fake.instances["c1"]["config"]["limits.memory"] = "2GiB"
    browser.post(url, {"new_name": "c2", "target": "node2"})
    copy = fake.instances["c2"]
    assert copy["location"] == "node2"
    # Incus does not fill in a copy on another member; the console sends it all.
    assert copy["config"]["limits.memory"] == "2GiB" and copy["profiles"] == ["default"]
    assert copy["config"]["image.description"] == "Debian 13"
    assert not any(key.startswith("volatile.") for key in copy["config"])
    assert "not a cluster member" in text(browser.post(url, {"new_name": "c3", "target": "x"}))
    assert "c3" not in fake.instances


def test_remove_a_member(browser, sid, fake):
    fake.add_instance("c1")
    fake.make_cluster("node1", "node2", "node3")
    base = f"/servers/{sid}/cluster/members"
    assert ">Remove<" in text(browser.get(f"{base}/node3"))
    kept = browser.post(f"{base}/node1/remove", follow_redirects=True)
    assert "still has instances" in text(kept) and "node1" in fake.members
    gone = browser.post(f"{base}/node3/remove", follow_redirects=True)
    assert "node3 removed from the cluster." in text(gone)
    assert "node3" not in fake.members and fake.groups["default"]["members"] == ["node1", "node2"]


def test_only_console_admins_remove_members(server_app, setup, fake):
    fake.make_cluster("node1", "node2")
    pam = login(server_app, "pam")
    assert pam.post(f"/servers/{setup}/cluster/members/node2/remove").status_code == 403
    assert "node2" in fake.members
