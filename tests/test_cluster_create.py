"""Starting a cluster, join tokens, and joining a registered server to it."""

import base64
import json
import re

import pytest

from app.views.cluster import listen_address

from .fake_incus import FakeIncus, serve_unix
from .test_app import text


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("10.0.0.5", "10.0.0.5:8443"),
        ("10.0.0.5:9000", "10.0.0.5:9000"),
        ("https://10.0.0.5:8443/", "10.0.0.5:8443"),
        ("fd00::5", "[fd00::5]:8443"),
        ("[fd00::5]:9000", "[fd00::5]:9000"),
        (":8443", None),
        ("0.0.0.0:8443", None),
        ("[::]:8443", None),
        ("10.0.0.5:99999", None),
        ("", None),
    ],
)
def test_listen_address(given, expected):
    assert listen_address(given) == expected


def test_a_standalone_server_offers_to_start_a_cluster(browser, sid, fake):
    page = text(browser.get(f"/servers/{sid}/cluster"))
    assert "Start a cluster" in page and 'value="fake"' in page
    assert "does not listen on the network yet" in page
    assert f"/servers/{sid}/cluster" in text(browser.get(f"/servers/{sid}/instances"))


def test_start_a_cluster(browser, sid, fake):
    done = browser.post(
        f"/servers/{sid}/cluster/enable",
        {"server_name": "node1", "address": "10.0.0.9"},
        follow_redirects=True,
    )
    page = text(done)
    assert "Cluster started" in page and "10.0.0.9:8443" in page
    assert fake.config["core.https_address"] == "10.0.0.9:8443"
    assert list(fake.members) == ["node1"]
    # Once clustered there is nothing to start, and members can be added.
    assert "Start a cluster" not in page and "Get a join token" in page
    again = browser.post(
        f"/servers/{sid}/cluster/enable",
        {"server_name": "node1", "address": "10.0.0.9"},
        follow_redirects=True,
    )
    assert "already part of a cluster" in text(again)


def test_a_cluster_needs_a_real_address_and_name(browser, sid, fake):
    for data in (
        {"server_name": "node1", "address": ":8443"},
        {"server_name": "node1", "address": ""},
        {"server_name": "-bad-", "address": "10.0.0.9"},
    ):
        browser.post(f"/servers/{sid}/cluster/enable", data)
    assert not fake.members and "core.https_address" not in fake.config


def test_join_tokens(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    page = text(browser.post(f"/servers/{sid}/cluster/tokens", {"name": "node3"}))
    token = re.search(r'<code class="token">([^<]+)</code>', page).group(1)
    data = json.loads(base64.b64decode(token))
    assert data["server_name"] == "node3" and data["addresses"] == ["10.0.0.1:8443"]
    assert data["secret"] and data["fingerprint"]
    assert "incus admin init --preseed" in page and "entity: storage-pool" in page
    listing = text(browser.get(f"/servers/{sid}/cluster"))
    assert "Join token for" in listing and token in listing
    # A name that is taken gets no token.
    taken = browser.post(f"/servers/{sid}/cluster/tokens", {"name": "node2"}, follow_redirects=True)
    assert "node2 is already a member" in text(taken)
    (op_id,) = [o["id"] for o in fake.operations.values() if o["class"] == "token"]
    done = browser.post(f"/servers/{sid}/cluster/tokens/{op_id}/revoke", follow_redirects=True)
    assert "revoked" in text(done) and token not in text(done)
    assert fake.operations[op_id]["status"] == "Cancelled"
    assert browser.post(f"/servers/{sid}/cluster/tokens/{op_id}/revoke").status_code == 404


def test_no_tokens_or_joining_without_a_cluster(browser, sid, fake):
    assert browser.post(f"/servers/{sid}/cluster/tokens", {"name": "x"}).status_code == 404
    assert browser.get(f"/servers/{sid}/cluster/join").status_code == 404


@pytest.fixture
def spare(browser, short_dir):
    """A second fake Incus, standing alone and registered as "Spare"; (fake, id)."""
    world = FakeIncus()
    server = serve_unix(world, f"{short_dir}/spare.sock")
    response = browser.post(
        "/servers", {"kind": "unix", "name": "Spare", "address": f"{short_dir}/spare.sock"}
    )
    yield world, int(re.search(r"/servers/(\d+)/", response.headers["Location"]).group(1))
    server.shutdown()
    server.server_close()


def test_join_a_registered_server(browser, sid, fake, spare):
    world, spare_id = spare
    fake.make_cluster("node1", "node2")
    world.cluster_of = fake
    pick = text(browser.get(f"/servers/{sid}/cluster/join"))
    assert "Spare" in pick and f"server={spare_id}" in pick
    form = text(browser.get(f"/servers/{sid}/cluster/join?server={spare_id}"))
    assert 'name="config-0"' in form and "source" in form and "storage pool" in form
    done = browser.post(
        f"/servers/{sid}/cluster/join",
        {"server": spare_id, "name": "node3", "address": "10.0.0.3", "config-0": "/srv/pool"},
        follow_redirects=True,
    )
    assert "Spare joined the cluster as node3." in text(done)
    assert fake.members["node3"]["url"] == "https://10.0.0.3:8443"
    assert "node3" in fake.groups["default"]["members"]
    (join,) = world.joins
    assert join["server_address"] == "10.0.0.3:8443" and join["server_name"] == "node3"
    assert join["member_config"][0]["key"] == "source"
    assert join["member_config"][0]["value"] == "/srv/pool"
    # Now part of the cluster, it is no longer offered.
    assert "already part of a cluster" in text(browser.get(f"/servers/{sid}/cluster/join"))


def test_a_failed_join_gives_the_token_back(browser, sid, fake, spare):
    world, spare_id = spare
    fake.make_cluster("node1")
    world.cluster_of = fake
    world.add_instance("busy")
    done = text(
        browser.post(
            f"/servers/{sid}/cluster/join",
            {"server": spare_id, "name": "node2", "address": "10.0.0.2", "config-0": ""},
        )
    )
    assert "Spare could not join: The joining server has instances" in done
    assert "node2" not in fake.members
    tokens = [o for o in fake.operations.values() if o["class"] == "token"]
    assert [o["status"] for o in tokens] == ["Cancelled"]


def test_joining_takes_a_free_name_and_an_address(browser, sid, fake, spare):
    world, spare_id = spare
    fake.make_cluster("node1")
    world.cluster_of = fake
    for data in ({"name": "node1", "address": "10.0.0.2"}, {"name": "node2", "address": ":8443"}):
        browser.post(f"/servers/{sid}/cluster/join", {"server": spare_id, "config-0": "", **data})
    assert list(fake.members) == ["node1"] and not world.joins
    assert browser.get(f"/servers/{sid}/cluster/join?server=999").status_code == 404
    assert browser.get(f"/servers/{sid}/cluster/join?server={sid}").status_code == 404


def test_only_console_admins_build_clusters(server_app, setup, fake):
    from .conftest import login

    pam = login(server_app, "pam")  # project admin on every project
    base = f"/servers/{setup}/cluster"
    assert "Start a cluster" not in text(pam.get(base))
    assert f'{base}"' not in text(pam.get(f"/servers/{setup}/instances"))
    enable = {"server_name": "node1", "address": "10.0.0.9"}
    assert pam.post(f"{base}/enable", enable).status_code == 403
    fake.make_cluster("node1", "node2")
    assert "Get a join token" not in text(pam.get(base))
    assert pam.post(f"{base}/tokens", {"name": "node3"}).status_code == 403
    assert pam.get(f"{base}/join").status_code == 403
    assert not [o for o in fake.operations.values() if o["class"] == "token"]
