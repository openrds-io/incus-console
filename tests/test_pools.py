"""Storage pools: create, details, delete; per member in a cluster."""

from .conftest import login
from .test_app import text


def test_create_and_delete_a_pool(browser, sid, fake):
    base = f"/servers/{sid}/storage"
    form = text(browser.get(f"{base}/new"))
    # Only drivers the server has, and only those the form can fill in.
    assert 'value="dir"' in form and 'value="btrfs"' in form
    assert 'value="lvm"' not in form and 'value="ceph"' not in form
    assert 'name="source"' in form and "Source on each member" not in form
    for bad, message in (
        ({"name": "bad name", "driver": "dir"}, "Use letters"),
        ({"name": "p1", "driver": "ceph"}, "Choose a storage driver"),
        ({"name": "p1", "driver": "zfs", "size": "lots"}, "Write a size"),
        ({"name": "p1", "driver": "dir", "size": "5GiB"}, "leave the size empty"),
    ):
        assert message in text(browser.post(f"{base}/new", bad))
    made = browser.post(
        f"{base}/new",
        {
            "name": "fast",
            "driver": "zfs",
            "size": "20GiB",
            "source": "/dev/sdb",
            "description": "NVMe",
        },
        follow_redirects=True,
    )
    assert "Storage pool fast created." in text(made)
    assert fake.pools["fast"]["config"] == {"size": "20GiB", "source": "/dev/sdb"}
    page = text(browser.get(f"{base}/fast"))
    assert "/dev/sdb" in page and "5.0 GiB used of 20.0 GiB" in page and "NVMe" in page
    assert f"{base}/fast" in text(browser.get(base))
    # A pool with something in it stays.
    fake.add_volume("fast", "data")
    assert "disabled" in text(browser.get(f"{base}/fast"))
    assert "currently in use" in text(browser.post(f"{base}/fast/delete", follow_redirects=True))
    del fake.volumes["fast"]["data"]
    gone = browser.post(f"{base}/fast/delete", follow_redirects=True)
    assert "Storage pool fast deleted." in text(gone) and "fast" not in fake.pools


def test_a_pool_in_a_cluster_is_defined_on_every_member(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    base = f"/servers/{sid}/storage"
    form = text(browser.get(f"{base}/new"))
    assert 'name="source:node1"' in form and 'name="source:node2"' in form
    made = browser.post(
        f"{base}/new",
        {"name": "fast", "driver": "zfs", "size": "20GiB", "source:node1": "/dev/sdb"},
        follow_redirects=True,
    )
    assert "Storage pool fast created." in text(made)
    assert [t for t in fake.targets if t[1] == "storage-pools"] == [
        ("POST", "storage-pools", "node1"),
        ("POST", "storage-pools", "node2"),
    ]
    assert fake.pools["fast"]["member_config"] == {
        "node1": {"size": "20GiB", "source": "/dev/sdb"},
        "node2": {"size": "20GiB"},
    }
    page = text(made)
    assert "On each member" in page and "/dev/sdb" in page
    assert "5.0 GiB used of 20.0 GiB" in page and "3.0 GiB used of 20.0 GiB" in page


def test_a_network_shows_its_state_on_each_member(browser, sid, fake):
    assert "On each member" not in text(browser.get(f"/servers/{sid}/networks/incusbr0"))
    fake.make_cluster("node1", "node2")
    fake.members["node2"]["status"] = "Offline"
    page = text(browser.get(f"/servers/{sid}/networks/incusbr0"))
    assert "On each member" in page and "10.0.3.1/24" in page and "Offline" in page
    assert "On each member" not in text(browser.get(f"/servers/{sid}/networks/eth0"))


def test_only_console_admins_create_and_delete_pools(server_app, setup, admin, fake):
    pam = login(server_app, "pam")
    base = f"/servers/{setup}/storage"
    assert "Create pool" not in text(pam.get(base))
    assert pam.get(f"{base}/new").status_code == 403
    assert pam.post(f"{base}/new", {"name": "mine", "driver": "dir"}).status_code == 403
    assert pam.post(f"{base}/default/delete").status_code == 403
    assert "default" in text(pam.get(f"{base}/default")) and ">Delete<" not in text(
        pam.get(f"{base}/default")
    )
    assert "Create pool" in text(admin.get(base))
