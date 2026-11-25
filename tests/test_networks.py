"""Networks, DHCP leases and port forwards."""

from app.views.networks import used_by

from .test_app import text


def test_used_by():
    urls = ["/1.0/instances/web-1", "/1.0/profiles/default?project=p", "/1.0/storage-pools/x"]
    assert used_by(urls) == {"instances": ["web-1"], "profiles": ["default"]}


def test_list_and_detail(browser, sid, fake):
    fake.add_instance("web-1")
    page = text(browser.get(f"/servers/{sid}/networks"))
    assert page.index(">incusbr0<") < page.index(">eth0<")  # managed first
    assert "10.0.3.1/24" in page
    detail = text(browser.get(f"/servers/{sid}/networks/incusbr0"))
    assert "10.0.3.1/24 (NAT)" in detail
    assert f'href="/servers/{sid}/instances/web-1"' in detail
    assert f'href="/servers/{sid}/profiles/default"' in detail
    assert "<code>10.0.3.10</code>" in detail  # its DHCP lease
    assert "disabled" in detail  # in use: no deleting


def test_unmanaged_network_is_read_only(browser, sid):
    page = text(browser.get(f"/servers/{sid}/networks/eth0"))
    assert "not managed by Incus" in page and "Edit settings" not in page
    refused = browser.get(f"/servers/{sid}/networks/eth0/settings", follow_redirects=True)
    assert "cannot be changed here" in text(refused)


def test_create_edit_rename_delete(browser, sid, fake):
    url = f"/servers/{sid}/networks/new"
    form = text(browser.get(url))
    assert 'value="auto"' in form and "limits.cpu" not in form
    assert "at most 15" in text(browser.post(url, {"name": "a-very-long-bridge-name"}))
    created = {
        "name": "lab0",
        "description": "Lab",
        "cfg:ipv4.address": "auto",
        "cfg:ipv4.nat": "true",
        "cfg:ipv6.address": "none",
    }
    done = browser.post(url, created, follow_redirects=True)
    assert "Network lab0 created." in text(done)
    assert fake.networks["lab0"]["config"]["ipv6.address"] == "none"

    settings = f"/servers/{sid}/networks/lab0/settings"
    assert "briefly interrupts" in text(browser.get(settings))
    changed = {"description": "Lab", "cfg:ipv4.address": "10.50.0.1/24", "cfg:dns.domain": "lab"}
    browser.post(settings, changed)
    assert fake.networks["lab0"]["config"] == {"ipv4.address": "10.50.0.1/24", "dns.domain": "lab"}

    edit = f"/servers/{sid}/networks/lab0/edit"
    assert "incus network edit lab0" in text(browser.get(edit))
    browser.post(edit, {"yaml": "description: YAML\nconfig:\n  ipv4.address: none\n"})
    assert fake.networks["lab0"]["description"] == "YAML"

    renamed = browser.post(f"/servers/{sid}/networks/lab0/rename", {"new_name": "lab1"})
    assert renamed.headers["Location"].endswith("/networks/lab1")
    gone = browser.post(f"/servers/{sid}/networks/lab1/delete", follow_redirects=True)
    assert "Network lab1 deleted." in text(gone)
    assert "lab1" not in fake.networks


def test_delete_in_use_is_refused(browser, sid, fake):
    done = browser.post(f"/servers/{sid}/networks/incusbr0/delete", follow_redirects=True)
    assert "in use" in text(done)
    assert "incusbr0" in fake.networks


def test_forwards(browser, sid, fake):
    fake.add_instance("web-1")
    url = f"/servers/{sid}/networks/incusbr0/forwards/new"
    page = text(browser.get(url))
    assert '<option value="10.0.3.10">' in page  # leases offered as targets
    assert "must be an IP address" in text(browser.post(url, {"listen_address": "host"}))
    no_target = {"listen_address": "192.168.1.20", "listen_port_0": "443"}
    assert "needs a target address" in text(browser.post(url, no_target))
    bad_port = {**no_target, "target_address": "10.0.3.10", "listen_port_0": "http"}
    assert "is not a port" in text(browser.post(url, bad_port))
    form = {
        "listen_address": "192.168.1.20",
        "description": "Web",
        "target_address": "10.0.3.10",
        "protocol_0": "tcp",
        "listen_port_0": "443",
        "protocol_1": "udp",
        "listen_port_1": "8000-8010",
        "target_address_1": "10.0.3.11",
        "target_port_1": "9000-9010",
    }
    done = browser.post(url, form, follow_redirects=True)
    assert "Forward on 192.168.1.20 added." in text(done)
    saved = fake.forwards["incusbr0"]["192.168.1.20"]
    assert saved["config"] == {"target_address": "10.0.3.10"}
    assert saved["ports"] == [
        {
            "protocol": "tcp",
            "listen_port": "443",
            "target_address": "10.0.3.10",
            "description": "",
        },
        {
            "protocol": "udp",
            "listen_port": "8000-8010",
            "target_address": "10.0.3.11",
            "description": "",
            "target_port": "9000-9010",
        },
    ]
    detail = text(browser.get(f"/servers/{sid}/networks/incusbr0"))
    assert "udp 8000-8010 → 10.0.3.11:9000-9010" in detail

    edit = f"/servers/{sid}/networks/incusbr0/forwards/192.168.1.20"
    page = text(browser.get(edit))
    assert 'value="8000-8010"' in page and 'value="10.0.3.11"' in page
    browser.post(edit, {"target_address": "10.0.3.10", "listen_port_0": "80"})
    assert [p["listen_port"] for p in fake.forwards["incusbr0"]["192.168.1.20"]["ports"]] == ["80"]

    refused = browser.post(url, {**form, "listen_address": "10.0.3.50"})
    assert "subnet" in text(refused)
    gone = browser.post(f"{edit}/delete", follow_redirects=True)
    assert "Forward on 192.168.1.20 removed." in text(gone)
    assert fake.forwards["incusbr0"] == {}
