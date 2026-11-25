"""Virtual machines: create, settings and the text console."""

import threading

import pytest

from app import catalog

from .conftest import login
from .test_app import text
from .test_images import INDEX, with_catalog  # noqa: F401  (with_catalog is a fixture)


def create(browser, sid, **form):
    data = {"kind": "vm", "source": "remote", "alias": "debian/13", "start": "1", **form}
    return browser.post(f"/servers/{sid}/create-instance", data, follow_redirects=True)


def test_catalog_lists_vm_images_for_vms():
    (debian,) = catalog.container_images(INDEX, ["aarch64"], "virtual-machine")
    assert [i["label"] for i in debian["images"]] == ["Debian forky"]
    containers = catalog.container_images(INDEX, ["aarch64"])
    assert "Debian forky" not in str(containers)


def test_vm_form_offers_vm_images(browser, sid, fake, with_catalog):  # noqa: F811
    form = text(browser.get(f"/servers/{sid}/create-instance?type=vm"))
    assert "Create virtual machine" in form and "Debian forky" in form
    assert "Debian trixie" not in form and "run as virtual machines" in form
    assert "Secure Boot" in form and 'name="disk_size" value="20GiB"' in form


def test_create_vm(browser, sid, fake):
    page = text(
        create(browser, sid, name="vm1", cpu="2", memory="4GiB", disk_size="20GiB", secureboot="1")
    )
    assert "Virtual machine vm1 created." in page
    sent = fake.created[-1]
    assert sent["type"] == "virtual-machine"
    assert sent["source"]["alias"] == "debian/13" and sent["source"]["protocol"] == "simplestreams"
    assert sent["config"] == {"limits.cpu": "2", "limits.memory": "4GiB"}
    # The disk size needs a root device of its own: on the profiles' pool.
    assert sent["devices"] == {
        "root": {"type": "disk", "path": "/", "pool": "default", "size": "20GiB"}
    }
    assert "Virtual machine" in page and ">Console<" in page


def test_create_vm_without_secure_boot_on_a_chosen_pool(browser, sid, fake):
    create(browser, sid, name="old", pool="default", disk_size="", user_data="#cloud-config")
    sent = fake.created[-1]
    assert sent["config"]["security.secureboot"] == "false"
    assert sent["config"]["cloud-init.user-data"] == "#cloud-config"
    assert sent["devices"]["root"] == {"type": "disk", "path": "/", "pool": "default"}


def test_bad_disk_size_keeps_the_form(browser, sid, fake):
    page = text(create(browser, sid, name="vm1", disk_size="big", secureboot="1"))
    assert "is not a size" in page and 'value="big"' in page
    assert fake.created == []


def test_local_vm_images_and_image_page(browser, sid, fake):
    container = fake.add_image("Debian 13 container")
    vm = fake.add_image("Debian 13 VM", kind="virtual-machine")
    vm_form = text(browser.get(f"/servers/{sid}/create-instance?type=vm"))
    assert vm in vm_form and container not in vm_form
    assert vm not in text(browser.get(f"/servers/{sid}/create-instance"))
    assert "Create VM" in text(browser.get(f"/servers/{sid}/images/{vm}"))
    form = text(browser.get(f"/servers/{sid}/create-instance?image={vm}"))
    assert 'name="kind" value="vm"' in form


def test_vm_settings(browser, sid, fake):
    fake.add_instance("vm1", kind="virtual-machine")
    url = f"/servers/{sid}/instances/vm1/settings"
    form = text(browser.get(url))
    assert "security.secureboot" in form and "Default (yes)" in form
    assert "security.nesting" not in form and "limits.processes" not in form
    browser.post(url, {"description": "", "cfg:security.secureboot": "false"})
    assert fake.instances["vm1"]["config"]["security.secureboot"] == "false"
    fake.add_instance("c1")
    form = text(browser.get(f"/servers/{sid}/instances/c1/settings"))
    assert "security.nesting" in form and "security.secureboot" not in form


def test_buttons_by_kind(browser, sid, fake):
    fake.add_instance("vm1", kind="virtual-machine")
    fake.add_instance("c1")
    vm = text(browser.get(f"/servers/{sid}/instances/vm1"))
    assert ">Console<" in vm and ">Terminal<" in vm
    assert ">Console<" in text(browser.get(f"/servers/{sid}/instances/c1"))


def test_text_console_page(browser, sid, fake):
    fake.add_instance("vm1", kind="virtual-machine")
    page = text(browser.get(f"/servers/{sid}/instances/vm1/console"))
    assert "Console - vm1" in page and "/console/ws" in page and "without the Incus agent" in page


def test_consoles_are_operator_work(server_app, setup, fake):
    vic = login(server_app, "vic")
    base = f"/servers/{setup}/instances/web-1"
    assert vic.get(f"{base}/console").status_code == 403
    assert login(server_app, "otto").get(f"{base}/console").status_code == 200


def _serve(app):
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_text_console_relays_like_the_terminal(app, sid, fake):
    pytest.importorskip("simple_websocket")
    websocket = pytest.importorskip("websocket")
    import json

    fake.add_instance("vm1", kind="virtual-machine")
    server = _serve(app)
    try:
        ws = websocket.create_connection(
            f"ws://127.0.0.1:{server.server_port}/servers/{sid}/instances/vm1/console/ws",
            timeout=5,
        )
        # First what the console printed before we attached, then the live console.
        assert ws.recv() == b"Debian GNU/Linux 13 vm1 console\r\n<b>login:</b> "
        ws.send(json.dumps({"t": "i", "d": "exit"}))
        assert ws.recv() == b"exit"
        assert json.loads(ws.recv()) == {"t": "x"}
        ws.close()
    finally:
        server.shutdown()
    (session,) = fake.sessions.values()
    assert session["request"]["type"] == "console"


def test_create_button_shows_that_it_is_working(browser, sid, fake):
    form = text(browser.get(f"/servers/{sid}/create-instance?type=vm"))
    assert 'data-busy="Creating…"' in form and "several hundred MB" in form


def test_console_replays_earlier_output():
    from app import terminal
    from app.incus import IncusError

    class Browser:
        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(data)

    class Client:
        def __init__(self, log):
            self.log = log

        def console_log(self, name):
            if isinstance(self.log, Exception):
                raise self.log
            return self.log

    browser = Browser()
    terminal._replay(Client("boot\nok\r\nlogin: "), browser, "vm1")
    assert browser.sent == [b"boot\r\nok\r\nlogin: "]
    long = "x" * (terminal.REPLAY_BYTES + 10)
    browser = Browser()
    terminal._replay(Client(long), browser, "vm1")
    assert len(browser.sent[0]) == terminal.REPLAY_BYTES  # only the end
    for quiet in ("", IncusError(400, "Console log not available")):
        browser = Browser()
        terminal._replay(Client(quiet), browser, "vm1")
        assert browser.sent == []
