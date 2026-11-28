"""Upgrading Incus across a cluster, and renewing the cluster certificate."""

import pytest

from app.certs import check_pair, describe, new_pair
from app.views.cluster_upkeep import version_key

from .test_app import text


def test_versions_sort_by_number():
    assert sorted(["7.10", "7.9", "6.0", "7.9.1"], key=version_key) == [
        "6.0",
        "7.9",
        "7.9.1",
        "7.10",
    ]


def test_an_upgrade_in_progress(browser, sid, fake):
    fake.make_cluster("node1", "node2", "node3")
    fake.members["node1"]["version"] = "6.1"
    fake.members["node2"].update(status="Blocked", message="waiting for other members")
    page = text(browser.get(f"/servers/{sid}/cluster/upgrade"))
    assert "Every member runs" not in page
    assert "On node3, run <code>sudo apt update" in page
    assert "Upgraded; it waits for the others" in page
    rows = page.split("<tbody>")[1]
    assert rows.index("node1") < rows.index("node2") < rows.index("node3")
    listing = text(browser.get(f"/servers/{sid}/cluster"))
    assert "/cluster/upgrade" in listing


def test_an_upgrade_that_is_done(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    page = text(browser.get(f"/servers/{sid}/cluster/upgrade"))
    assert "Every member runs Incus 6.0." in page and "Up to date" in page


def test_new_pairs_go_together_and_others_do_not():
    cert, key = new_pair("incus-cluster")
    assert check_pair(cert, key) is None
    assert describe(cert)["subject"] == "CN=incus-cluster"
    _other_cert, other_key = new_pair("other")
    assert "does not belong" in check_pair(cert, other_key)
    assert "not a PEM certificate" in check_pair("nonsense", key)
    assert "passphrase" in check_pair(cert, "nonsense")


def test_renew_the_cluster_certificate(app, browser, sid, fake):
    from app import servers

    fake.make_cluster("node1", "node2")
    with app.app_context():
        member = servers.add("node2", "https", "https://10.0.0.2:8443", "aa" * 32)
        elsewhere = servers.add("other", "https", "https://10.9.9.9:8443", "aa" * 32)
    before = text(browser.get(f"/servers/{sid}/cluster/certificate"))
    assert "Renew it" in before
    done = browser.post(
        f"/servers/{sid}/cluster/certificate", {"source": "generate"}, follow_redirects=True
    )
    assert "Every member now uses the new cluster certificate." in text(done)
    (renewal,) = fake.renewed
    new = describe(renewal["cluster_certificate"])
    assert new["subject"] == "CN=incus-cluster"
    with app.app_context():
        assert servers.get(member)["fingerprint"] == new["fingerprint"]
        assert servers.get(elsewhere)["fingerprint"] == "aa" * 32


@pytest.mark.parametrize("pasted", [("nonsense", "nonsense"), None])
def test_a_pasted_certificate_is_checked(browser, sid, fake, pasted):
    fake.make_cluster("node1", "node2")
    if pasted is None:
        cert, _key = new_pair("a")
        _cert, key = new_pair("b")
        pasted = (cert, key)
    data = {"source": "paste", "certificate": pasted[0], "key": pasted[1]}
    browser.post(f"/servers/{sid}/cluster/certificate", data)
    assert fake.renewed == []


def test_upkeep_pages_are_for_admins_and_clusters(server_app, setup, fake):
    from .conftest import login

    admin = login(server_app, "root")
    for page in ("upgrade", "certificate"):
        assert admin.get(f"/servers/{setup}/cluster/{page}").status_code == 404
    fake.make_cluster("node1", "node2")
    pam = login(server_app, "pam")
    for page in ("upgrade", "certificate"):
        assert pam.get(f"/servers/{setup}/cluster/{page}").status_code == 403
    renew = pam.post(f"/servers/{setup}/cluster/certificate", {"source": "generate"})
    assert renew.status_code == 403
    assert fake.renewed == []
