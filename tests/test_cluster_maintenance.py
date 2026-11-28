"""Looking after a cluster: renaming and force-removing members, versions and database roles."""

from .test_app import text


def test_members_show_their_database_part_and_version(browser, sid, fake):
    fake.make_cluster("node1", "node2", "node3")
    fake.members["node3"]["roles"] = ["database-standby"]
    fake.members["node2"]["roles"] = []
    page = text(browser.get(f"/servers/{sid}/cluster"))
    for label in ("<td>Leader</td>", "<td>Spare</td>", "<td>Stand-by</td>", "<td>6.0</td>"):
        assert label in page
    assert "different versions" not in page
    member = text(browser.get(f"/servers/{sid}/cluster/members/node1"))
    assert "<th>Database</th><td>Leader</td>" in member and "<th>Incus</th><td>6.0</td>" in member


def test_mixed_versions_are_pointed_out(browser, sid, fake):
    fake.make_cluster("node1", "node2", "node3")
    fake.members["node3"]["version"] = "6.1"
    page = text(browser.get(f"/servers/{sid}/cluster"))
    assert "different versions of Incus: 6.0 on node1, node2; 6.1 on node3" in page


def test_an_offline_member_has_no_version_and_shows_why(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    fake.members["node2"].update(status="Offline", message="No heartbeat for 30s")
    page = text(browser.get(f"/servers/{sid}/cluster"))
    assert "No heartbeat for 30s" in page and "different versions" not in page


def test_rename_a_member(browser, sid, fake):
    fake.add_instance("c1")
    fake.make_cluster("node1", "node2")
    form = {"name": "web1", "description": "", "failure_domain": "default", "groups": "default"}
    done = browser.post(f"/servers/{sid}/cluster/members/node1/edit", form, follow_redirects=True)
    assert "node1 is now web1." in text(done)
    assert list(fake.members) == ["web1", "node2"]
    assert fake.instances["c1"]["location"] == "web1"
    assert sorted(fake.groups["default"]["members"]) == ["node2", "web1"]
    taken = browser.post(f"/servers/{sid}/cluster/members/web1/edit", {**form, "name": "node2"})
    assert "node2 is already a member" in text(taken) and "web1" in fake.members
    bad = browser.post(f"/servers/{sid}/cluster/members/web1/edit", {**form, "name": "-x-"})
    assert "like a host name" in text(bad) and "web1" in fake.members


def test_force_remove_is_for_an_offline_member(browser, sid, fake):
    fake.add_instance("c1")
    fake.make_cluster("node1", "node2")
    fake.instances["c1"]["location"] = "node2"
    base = f"/servers/{sid}/cluster/members/node2"
    page = text(browser.get(base))
    assert ">Remove<" in page and "Force remove" not in page
    refused = browser.post(f"{base}/remove", {"force": "1"}, follow_redirects=True)
    assert "remove it normally" in text(refused) and "node2" in fake.members
    fake.members["node2"]["status"] = "Offline"
    assert "Force remove" in text(browser.get(base))
    done = browser.post(f"{base}/remove", {"force": "1"}, follow_redirects=True)
    assert "node2 forgotten by the cluster." in text(done)
    assert "node2" not in fake.members and "c1" not in fake.instances


def test_only_console_admins_rename(server_app, setup, fake):
    from .conftest import login

    fake.make_cluster("node1", "node2")
    pam = login(server_app, "pam")
    form = {"name": "web1", "failure_domain": "default", "groups": "default"}
    assert pam.post(f"/servers/{setup}/cluster/members/node1/edit", form).status_code == 403
    assert "node1" in fake.members


def test_a_member_without_its_database_is_not_called_standalone(browser, sid, fake, monkeypatch):
    from app.incus import IncusClient, IncusError

    fake.make_cluster("node1", "node2")

    def lost(self):
        raise IncusError(500, "no available cowsql leader server found")

    monkeypatch.setattr(IncusClient, "cluster_members", lost)
    page = text(browser.get(f"/servers/{sid}/cluster"))
    assert "cannot say which cluster it belongs to: no available cowsql leader" in page
    assert "is not part of a cluster" not in page and "Start a cluster" not in page
