"""The image catalog, the create form's image choices, and the Images pages."""

import pytest

from app import catalog

from .test_app import text


def product(os_name, release, title, variant, arch="arm64", files=("squashfs", "incus.tar.xz")):
    alias = f"{os_name.lower()}/{release}"
    aliases = f"{alias}/{variant},{alias}" if variant == "default" else f"{alias}/{variant}"
    items = {f"f{n}": {"ftype": ftype} for n, ftype in enumerate(files)}
    return {
        "aliases": aliases,
        "arch": arch,
        "os": os_name,
        "release": release,
        "release_title": title,
        "variant": variant,
        "versions": {"20261125_00:00": {"items": items}},
    }


INDEX = {
    "products": {
        "d12": product("Debian", "12", "bookworm", "default"),
        "d13": product("Debian", "13", "trixie", "default"),
        "d13c": product("Debian", "13", "trixie", "cloud"),
        "d13x86": product("Debian", "13", "trixie", "default", arch="amd64"),
        "d13vm": product("Debian", "14", "forky", "default", files=("disk-kvm.img",)),
        "alma": product("AlmaLinux", "10", "10", "default"),
    }
}


def test_container_images():
    groups = catalog.container_images(INDEX, ["aarch64"])
    assert [g["os"] for g in groups] == ["AlmaLinux", "Debian"]
    debian = groups[1]["images"]
    assert [i["label"] for i in debian] == [
        "Debian trixie",
        "Debian trixie (cloud)",
        "Debian bookworm",
    ]
    assert debian[0]["alias"] == "debian/13"
    assert debian[1]["alias"] == "debian/13/cloud"
    assert catalog.container_images(INDEX, ["x86_64"])[0]["images"][0]["label"] == "Debian trixie"


def test_fetch_needs_https():
    with pytest.raises(ValueError, match="https"):
        catalog.fetch("http://images.example")


@pytest.fixture
def with_catalog(app, monkeypatch):
    catalog.clear()
    calls = []

    def fake_fetch(server, timeout=20):
        calls.append(server)
        return INDEX

    monkeypatch.setattr(catalog, "fetch", fake_fetch)
    app.config["IMAGE_CATALOG"] = True
    yield calls
    catalog.clear()


def test_create_from_catalog(browser, sid, fake, with_catalog):
    url = f"/servers/{sid}/create-instance"
    page = text(browser.get(url))
    assert '<optgroup label="Debian">' in page
    assert '<option value="debian/13/cloud">Debian trixie (cloud)</option>' in page
    assert '<option value="debian/13" selected>' in page
    browser.get(url)
    assert with_catalog == ["https://images.linuxcontainers.org"]  # fetched once, then cached

    form = {
        "name": "c1",
        "source": "catalog",
        "catalog_alias": "debian/13/cloud",
        "alias": "ignored",
        "pool": "default",
        "user_data": "#cloud-config\npackages: [nginx]",
    }
    done = browser.post(url, form, follow_redirects=True)
    assert "Container c1 created." in text(done)
    sent = fake.created[-1]
    assert sent["source"]["alias"] == "debian/13/cloud"
    assert sent["source"]["server"] == "https://images.linuxcontainers.org"
    assert sent["config"]["cloud-init.user-data"].startswith("#cloud-config")
    assert sent["devices"] == {"root": {"type": "disk", "path": "/", "pool": "default"}}


def test_catalog_offline(browser, sid, app, monkeypatch):
    catalog.clear()

    def offline(server, timeout=20):
        raise OSError("no network")

    monkeypatch.setattr(catalog, "fetch", offline)
    app.config["IMAGE_CATALOG"] = True
    page = text(browser.get(f"/servers/{sid}/create-instance"))
    assert "could not be loaded" in page
    assert 'value="remote" checked' in page
    catalog.clear()


def test_create_from_other_server_and_no_pool(browser, sid, fake):
    form = {"name": "c2", "source": "remote", "alias": "ubuntu/26.04", "image_server": ""}
    browser.post(f"/servers/{sid}/create-instance", form)
    sent = fake.created[-1]
    assert sent["source"]["alias"] == "ubuntu/26.04"
    assert "devices" not in sent and "cloud-init.user-data" not in sent["config"]
    empty = browser.post(f"/servers/{sid}/create-instance", {"name": "c3", "source": "remote"})
    assert "Choose an image." in text(empty)


def test_images_pages(browser, sid, fake):
    assert "No images on this server yet" in text(browser.get(f"/servers/{sid}/images"))
    fp = fake.add_image("Web <base>", ["web-base"])
    page = text(browser.get(f"/servers/{sid}/images"))
    assert "Web &lt;base&gt;" in page and "<code>web-base</code>" in page
    detail = text(browser.get(f"/servers/{sid}/images/{fp}"))
    assert fp in detail and f"create-instance?image={fp}" in detail
    form = text(browser.get(f"/servers/{sid}/create-instance?image={fp}"))
    assert 'value="local" checked' in form and f'value="{fp}" selected' in form
    assert browser.get(f"/servers/{sid}/images/{'0' * 64}").status_code == 404


def test_aliases(browser, sid, fake):
    fp = fake.add_image("Base")
    url = f"/servers/{sid}/images/{fp}/aliases"
    assert "no spaces" in text(browser.post(url, {"alias": "has space"}, follow_redirects=True))
    done = browser.post(url, {"alias": "team/base", "description": "d"}, follow_redirects=True)
    assert "Alias team/base added." in text(done)
    assert fake.images[fp]["aliases"] == [{"name": "team/base", "description": "d"}]
    assert "already exists" in text(
        browser.post(url, {"alias": "team/base"}, follow_redirects=True)
    )
    gone = browser.post(f"{url}/team/base/delete", follow_redirects=True)
    assert "Alias team/base removed." in text(gone)
    assert fake.images[fp]["aliases"] == []


def test_delete_image(browser, sid, fake):
    fp = fake.add_image("Old")
    done = browser.post(f"/servers/{sid}/images/{fp}/delete", follow_redirects=True)
    assert "Image deleted." in text(done)
    assert fp not in fake.images


def test_publish(browser, sid, fake):
    fake.add_instance("web-1")
    url = f"/servers/{sid}/instances/web-1/publish"
    page = text(browser.get(url))
    assert "Stop web-1 first" in page and 'value="web-1 (Debian 13)"' in page
    assert "stop it first" in text(browser.post(url, {"alias": "web"}))
    fake.instances["web-1"]["status"] = "Stopped"
    done = browser.post(url, {"alias": "web-base", "description": "Web"}, follow_redirects=True)
    assert "Image of web-1 published." in text(done)
    assert fake.published[-1]["aliases"] == [{"name": "web-base"}]
    assert fake.published[-1]["properties"] == {"description": "Web"}
    [image] = fake.images.values()
    assert image["cached"] is False
    assert "no spaces" in text(browser.post(url, {"alias": "has space"}))


def test_slow_create_goes_to_operations(browser, sid, fake):
    # Incus makes the instance only once the image is fetched; until then the
    # new instance has no page, so a slow create lands on Operations.
    fake.pending_ops = True
    form = {"name": "r10", "source": "remote", "alias": "rockylinux/10"}
    response = browser.post(f"/servers/{sid}/create-instance", form)
    assert response.headers["Location"] == f"/servers/{sid}/operations"
    fake.add_instance("web-1")
    copied = browser.post(f"/servers/{sid}/instances/web-1/copy", {"new_name": "web-2"})
    assert copied.headers["Location"] == f"/servers/{sid}/operations"
