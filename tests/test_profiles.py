"""Profiles: list, create, copy, settings, YAML, devices, rename, delete."""

from app.views.profiles import instance_names

from .test_app import text


def test_instance_names():
    used_by = ["/1.0/instances/web-1?project=x", "/1.0/instances/a%2Fb", "/1.0/profiles/p"]
    assert instance_names(used_by) == ["a/b", "web-1"]
    assert instance_names(None) == []


def test_list_and_detail(browser, sid, fake):
    fake.add_instance("web-1")
    page = text(browser.get(f"/servers/{sid}/profiles"))
    assert page.index(">default<") < page.index(">big<")
    assert "1 instance<" in page
    detail = text(browser.get(f"/servers/{sid}/profiles/default"))
    assert "Default Incus profile" in detail
    assert f'href="/servers/{sid}/instances/web-1"' in detail
    assert "network=incusbr0" in detail
    assert ">Rename<" not in detail and "Delete</button>" not in detail  # default stays
    assert browser.get(f"/servers/{sid}/profiles/ghost").status_code == 404


def test_create_and_copy(browser, sid, fake):
    url = f"/servers/{sid}/profiles/new"
    assert "A copy of big" in text(browser.get(url))
    assert "letters, digits" in text(browser.post(url, {"name": "bad name!"})).lower()
    done = browser.post(url, {"name": "web", "description": "Web tier"}, follow_redirects=True)
    assert "Profile web created." in text(done)
    assert fake.profiles["web"]["description"] == "Web tier"
    assert fake.profiles["web"]["devices"] == {}

    assert "selected>A copy of default" in text(browser.get(f"{url}?copy_from=default"))
    browser.post(url, {"name": "base", "copy_from": "default"})
    assert fake.profiles["base"]["devices"] == fake.profiles["default"]["devices"]
    assert "already exists" in text(browser.post(url, {"name": "base"}))


def test_settings_and_yaml(browser, sid, fake):
    url = f"/servers/{sid}/profiles/big/settings"
    page = text(browser.get(url))
    assert 'value="4"' in page and "From profile" not in page
    form = {"description": "Big ones", "cfg:limits.cpu": "", "cfg:limits.memory": "8GiB"}
    assert "Profile big updated." in text(browser.post(url, form, follow_redirects=True))
    assert fake.profiles["big"]["config"] == {"limits.memory": "8GiB"}
    assert fake.profiles["big"]["description"] == "Big ones"

    edit = f"/servers/{sid}/profiles/big/edit"
    assert "incus profile edit big" in text(browser.get(edit))
    assert "Invalid YAML" in text(browser.post(edit, {"yaml": "config: [oops"}))
    new = "description: Huge\nconfig:\n  limits.cpu: '16'\ndevices: {}\nname: ignored\n"
    browser.post(edit, {"yaml": new})
    assert fake.profiles["big"]["config"] == {"limits.cpu": "16"}
    assert fake.profiles["big"]["name"] == "big"


def test_devices(browser, sid, fake):
    url = f"/servers/{sid}/profiles/big/devices/new"
    assert "Every instance using this profile gets the device" in text(browser.get(url))
    form = {"device": "data", "type": "disk", "settings": "source=/srv/data\npath=/data"}
    assert "Device data added." in text(browser.post(url, form, follow_redirects=True))
    assert fake.profiles["big"]["devices"]["data"] == {
        "type": "disk",
        "source": "/srv/data",
        "path": "/data",
    }
    assert "already has a device named data" in text(browser.post(url, form))
    base = f"/servers/{sid}/profiles/big/devices"
    browser.post(f"{base}/data/delete")
    assert fake.profiles["big"]["devices"] == {}
    missing = browser.post(f"{base}/nope/delete", follow_redirects=True)
    assert "has no device named nope" in text(missing)


def test_rename_and_delete(browser, sid, fake):
    fake.add_instance("web-1")
    fake.instances["web-1"]["profiles"] = ["default", "big"]
    url = f"/servers/{sid}/profiles/big/rename"
    done = browser.post(url, {"new_name": "large"}, follow_redirects=True)
    assert "big renamed to large." in text(done)
    assert fake.instances["web-1"]["profiles"] == ["default", "large"]

    busy = browser.post(f"/servers/{sid}/profiles/large/delete", follow_redirects=True)
    assert "in use" in text(busy)
    fake.instances["web-1"]["profiles"] = ["default"]
    gone = browser.post(f"/servers/{sid}/profiles/large/delete", follow_redirects=True)
    assert "Profile large deleted." in text(gone)
    assert "large" not in fake.profiles


def test_default_profile_is_protected(browser, sid):
    rename = browser.get(f"/servers/{sid}/profiles/default/rename", follow_redirects=True)
    assert "cannot be renamed" in text(rename)
    delete = browser.post(f"/servers/{sid}/profiles/default/delete", follow_redirects=True)
    assert "cannot be deleted" in text(delete)
