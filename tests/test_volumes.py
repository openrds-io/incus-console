"""Custom storage volumes."""

from .test_app import text


def test_list_shows_only_custom_volumes(browser, sid, fake):
    fake.add_instance("web-1")  # its own container volume is not listed
    assert "No custom volumes" in text(browser.get(f"/servers/{sid}/volumes"))
    fake.add_volume("default", "data", config={"size": "5GiB"}, description="<b>x</b>")
    page = text(browser.get(f"/servers/{sid}/volumes"))
    assert ">data<" in page and "5GiB" in page and "&lt;b&gt;x&lt;/b&gt;" in page
    assert ">web-1<" not in page


def test_create(browser, sid, fake):
    url = f"/servers/{sid}/volumes/new"
    assert 'value="default" selected' in text(browser.get(f"{url}?pool=default"))
    assert "Write a size" in text(
        browser.post(url, {"pool": "default", "name": "d", "size": "big"})
    )
    assert "Choose a storage pool" in text(browser.post(url, {"pool": "nope", "name": "d"}))
    form = {"pool": "default", "name": "data", "size": "10 GiB", "content_type": "filesystem"}
    done = browser.post(url, form, follow_redirects=True)
    assert "Volume data created." in text(done)
    assert fake.volumes["default"]["data"]["config"] == {"size": "10GiB"}
    assert "already exists" in text(browser.post(url, form))


def test_edit_rename_delete(browser, sid, fake):
    fake.add_volume("default", "data", config={"size": "5GiB", "block.filesystem": "ext4"})
    base = f"/servers/{sid}/storage/default/volumes/data"
    assert 'value="5GiB"' in text(browser.get(f"{base}/edit"))
    browser.post(f"{base}/edit", {"size": "20GiB", "description": "Bigger"})
    assert fake.volumes["default"]["data"]["config"] == {
        "size": "20GiB",
        "block.filesystem": "ext4",
    }
    refused = browser.post(f"{base}/edit", {"size": "1MiB"}, follow_redirects=True)
    assert "smaller than its contents" in text(refused)
    renamed = browser.post(f"{base}/rename", {"new_name": "data2"})
    assert renamed.headers["Location"].endswith("/storage/default/volumes/data2")
    gone = browser.post(
        f"/servers/{sid}/storage/default/volumes/data2/delete", follow_redirects=True
    )
    assert "Volume data2 deleted." in text(gone)
    assert fake.volumes["default"] == {}


def test_attach_and_detach(browser, sid, fake):
    fake.add_instance("web-1")
    fake.add_volume("default", "data")
    base = f"/servers/{sid}/storage/default/volumes/data"
    page = text(browser.get(base))
    assert '<option value="web-1">' in page
    no_path = browser.post(f"{base}/attach", {"instance": "web-1"}, follow_redirects=True)
    assert "path to mount it at" in text(no_path)
    clash = browser.post(
        f"{base}/attach",
        {"instance": "web-1", "path": "/data", "device": "root"},
        follow_redirects=True,
    )
    assert "already has a device named root" in text(clash)

    done = browser.post(
        f"{base}/attach", {"instance": "web-1", "path": "/srv/data"}, follow_redirects=True
    )
    assert "data attached to web-1." in text(done)
    assert fake.instances["web-1"]["devices"]["data"] == {
        "type": "disk",
        "pool": "default",
        "source": "data",
        "path": "/srv/data",
    }
    page = text(browser.get(base))
    assert "Detach" in page and 'title="Detach it first"' in page
    assert "still in use" in text(browser.post(f"{base}/delete", follow_redirects=True))

    detached = browser.post(f"{base}/detach/web-1", follow_redirects=True)
    assert "data detached from web-1." in text(detached)
    assert "data" not in fake.instances["web-1"]["devices"]
    nothing = browser.post(f"{base}/detach/web-1", follow_redirects=True)
    assert "from a profile" in text(nothing)


def test_block_volume_needs_no_path(browser, sid, fake):
    fake.add_instance("vm-1")
    fake.add_volume("default", "raw", content_type="block")
    base = f"/servers/{sid}/storage/default/volumes/raw"
    assert 'name="path"' not in text(browser.get(base))
    browser.post(f"{base}/attach", {"instance": "vm-1"})
    assert fake.instances["vm-1"]["devices"]["raw"] == {
        "type": "disk",
        "pool": "default",
        "source": "raw",
    }


def test_block_volumes_are_offered_to_vms_only(browser, sid, fake):
    """Incus attaches block (and ISO) volumes to virtual machines only."""
    fake.add_instance("c1")
    fake.add_instance("vm1", kind="virtual-machine")
    fake.add_volume("default", "raw", content_type="block")
    fake.add_volume("default", "data")
    block = text(browser.get(f"/servers/{sid}/storage/default/volumes/raw"))
    assert 'value="vm1"' in block and 'value="c1"' not in block
    filesystem = text(browser.get(f"/servers/{sid}/storage/default/volumes/data"))
    assert 'value="vm1"' in filesystem and 'value="c1"' in filesystem


def test_snapshots(browser, sid, fake):
    fake.add_volume("default", "data")
    base = f"/servers/{sid}/storage/default/volumes/data/snapshots"
    assert "Snapshot s1 created." in text(
        browser.post(base, {"snapshot": "s1"}, follow_redirects=True)
    )
    page = text(browser.get(f"/servers/{sid}/storage/default/volumes/data"))
    assert "<td>s1</td>" in page and "data/s1" not in page
    assert "restored to s1" in text(browser.post(f"{base}/s1/restore", follow_redirects=True))
    browser.post(f"{base}/s1/delete")
    assert fake.volumes["default"]["data"]["snapshots"] == []
    assert "Use letters" in text(
        browser.post(base, {"snapshot": "bad name"}, follow_redirects=True)
    )
