"""The console manages containers only: virtual machines on a server stay out of sight."""

from .test_app import text


def test_virtual_machines_are_not_listed_or_reachable(browser, sid, fake):
    fake.add_instance("c1")
    fake.add_instance("vm1", kind="virtual-machine")
    page = text(browser.get(f"/servers/{sid}/instances"))
    assert "c1" in page and "vm1" not in page
    assert browser.get(f"/servers/{sid}/instances/vm1").status_code == 404
    assert browser.get(f"/servers/{sid}/instances/c1").status_code == 200


def test_vm_images_are_not_listed_or_reachable(browser, sid, fake):
    container = fake.add_image("Debian 13")
    machine = fake.add_image("Debian 13 VM", kind="virtual-machine")
    page = text(browser.get(f"/servers/{sid}/images"))
    assert "Debian 13" in page and "Debian 13 VM" not in page
    assert browser.get(f"/servers/{sid}/images/{machine}").status_code == 404
    assert browser.get(f"/servers/{sid}/images/{container}").status_code == 200


def test_the_create_form_ignores_a_vm_request(browser, sid, fake):
    form = text(browser.get(f"/servers/{sid}/create-instance?type=vm"))
    assert "Create container" in form
    browser.post(
        f"/servers/{sid}/create-instance",
        {"kind": "vm", "name": "c9", "source": "remote", "alias": "debian/13"},
    )
    assert fake.created[-1]["type"] == "container"
