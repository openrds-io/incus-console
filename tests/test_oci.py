"""OCI application containers: created from a registry image, run by Incus 6.3+."""

import json
import re
import threading

import pytest

from app.views.instances import oci_source

from .test_app import text

DOCKER = "https://docker.io"


def create(browser, sid, **form):
    data = {"kind": "oci", "source": "remote", "start": "1", **form}
    return browser.post(f"/servers/{sid}/create-instance", data, follow_redirects=True)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("nginx:alpine", (DOCKER, "nginx:alpine")),
        ("jgraph/drawio", (DOCKER, "jgraph/drawio")),
        ("docker.io/library/nginx", (DOCKER, "library/nginx")),
        ("ghcr.io/owner/app:1.2", ("https://ghcr.io", "owner/app:1.2")),
        ("registry.local:5000/team/app", ("https://registry.local:5000", "team/app")),
        ("localhost/app", ("https://localhost", "app")),
        ("docker://quay.io/org/img", ("https://quay.io", "org/img")),
    ],
)
def test_oci_source(reference, expected):
    assert oci_source(reference, DOCKER) == expected


def test_create_from_docker_hub(browser, sid, fake):
    form = text(browser.get(f"/servers/{sid}/create-instance?type=oci"))
    assert "Create application container" in form and "nginx:alpine" in form
    assert "Cloud-init" not in form
    page = text(
        create(
            browser,
            sid,
            name="web",
            oci_image="nginx:alpine",
            environment="TZ=America/New_York\nNGINX_PORT=8080",
            memory="512MiB",
        )
    )
    assert "Application container web created." in page
    sent = fake.created[-1]
    assert sent["type"] == "container"
    assert sent["source"] == {
        "type": "image",
        "mode": "pull",
        "protocol": "oci",
        "server": DOCKER,
        "alias": "nginx:alpine",
    }
    assert sent["config"] == {
        "limits.memory": "512MiB",
        "environment.TZ": "America/New_York",
        "environment.NGINX_PORT": "8080",
    }
    # The instance page says what it is and what it runs, but not secret values.
    assert "Application container (OCI)" in page and "docker-entrypoint.sh" in page
    assert "<code>NGINX_PORT</code>" in page and "America/New_York" not in page
    assert "App (OCI)" in text(browser.get(f"/servers/{sid}/instances"))


def test_create_from_another_registry(browser, sid, fake):
    create(browser, sid, name="ha", oci_image="ghcr.io/home-assistant/home-assistant:stable")
    source = fake.created[-1]["source"]
    assert source["server"] == "https://ghcr.io"
    assert source["alias"] == "home-assistant/home-assistant:stable"


def test_create_problems_keep_the_form(browser, sid, fake):
    assert "Choose an image" in text(create(browser, sid, name="a", oci_image=""))
    page = text(create(browser, sid, name="a", oci_image="Bad Name"))
    assert "is not an image name" in page and 'value="Bad Name"' in page
    page = text(create(browser, sid, name="a", oci_image="nginx", environment="1X=2"))
    assert "is not a variable name" in page
    assert "Line 1" in text(create(browser, sid, name="a", oci_image="nginx", environment="X"))
    assert fake.created == []


def test_server_without_oci(browser, sid, fake):
    fake.extensions.clear()
    form = text(browser.get(f"/servers/{sid}/create-instance?type=oci"))
    assert "cannot run application containers" in form and "oci_image" not in form
    assert "cannot run application" in text(create(browser, sid, name="w", oci_image="nginx"))
    assert fake.created == []
    # System containers are unaffected.
    assert "debian/13" in text(browser.get(f"/servers/{sid}/create-instance"))


def test_local_images_are_offered_by_kind(browser, sid, fake):
    system = fake.add_image("Debian 13")
    app_image = fake.add_image("docker.io/library/redis (OCI)")
    fake.images[app_image]["properties"]["type"] = "oci"
    oci_form = text(browser.get(f"/servers/{sid}/create-instance?type=oci"))
    assert app_image in oci_form and system not in oci_form
    container_form = text(browser.get(f"/servers/{sid}/create-instance"))
    assert system in container_form and app_image not in container_form
    # Coming from an OCI image's page opens the OCI form with it chosen.
    form = text(browser.get(f"/servers/{sid}/create-instance?image={app_image}"))
    assert 'name="kind" value="oci"' in form
    create(browser, sid, name="cache", source="local", fingerprint=app_image)
    assert fake.created[-1]["source"] == {"type": "image", "fingerprint": app_image}


def test_settings_edit_command_and_environment(browser, sid, fake):
    create(browser, sid, name="web", oci_image="nginx", environment="A=1\nB=2")
    url = f"/servers/{sid}/instances/web/settings"
    form = text(browser.get(url))
    assert "oci.entrypoint" in form and "A=1\nB=2" in form
    browser.post(
        url,
        {
            "description": "",
            "cfg:oci.entrypoint": '["/bin/sleep", "infinity"]',
            "cfg:oci.uid": "101",
            "environment": "B=3\nC=4",
        },
    )
    config = fake.instances["web"]["config"]
    assert config["oci.entrypoint"] == '["/bin/sleep", "infinity"]'
    assert config["oci.uid"] == "101"
    assert "environment.A" not in config
    assert (config["environment.B"], config["environment.C"]) == ("3", "4")
    page = text(browser.post(url, {"environment": "no equals"}))
    assert "key=value" in page and config["environment.B"] == "3"


def test_system_container_settings_have_environment_but_no_oci_keys(browser, sid, fake):
    fake.add_instance("web-1")
    form = text(browser.get(f"/servers/{sid}/instances/web-1/settings"))
    assert "environment.*" in form and "oci.entrypoint" not in form


def test_terminal_runs_sh_in_application_containers(app, browser, sid, fake):
    pytest.importorskip("simple_websocket")
    websocket = pytest.importorskip("websocket")
    from werkzeug.serving import make_server

    create(browser, sid, name="web", oci_image="nginx")
    fake.add_instance("sys")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"127.0.0.1:{server.server_port}/servers/{sid}/instances"
    try:
        for name in ("web", "sys"):
            ws = websocket.create_connection(f"ws://{base}/{name}/terminal/ws", timeout=5)
            ws.send(json.dumps({"t": "i", "d": "exit"}))
            assert ws.recv() == b"exit"
            assert json.loads(ws.recv()) == {"t": "x"}
            ws.close()
    finally:
        server.shutdown()
    commands = [s["request"]["command"] for s in fake.sessions.values()]
    assert ["sh"] in commands and ["su", "-l"] in commands


def test_create_form_has_a_tab_per_kind(browser, sid, fake):
    assert "Create instance" in text(browser.get(f"/servers/{sid}/instances"))
    form = text(browser.get(f"/servers/{sid}/create-instance"))
    assert re.search(r"create-instance\?type=oci", form)
    assert "type=vm" not in form and "Virtual machine" not in form


def test_environment_values_are_hidden_from_viewers_and_operators(server_app, setup, admin, fake):
    from .conftest import login

    fake.add_instance("db")
    fake.instances["db"]["config"].update(
        {"volatile.container.oci": "true", "environment.DB_PASSWORD": "s3cret-value"}
    )
    fake.instances["db"]["expanded_config"].update(fake.instances["db"]["config"])
    url = f"/servers/{setup}/instances/db"
    for username in ("vic", "otto"):
        page = text(login(server_app, username).get(url))
        assert "DB_PASSWORD" in page and "s3cret-value" not in page
        assert "only project admins see them" in page
    assert "s3cret-value" in text(login(server_app, "pam").get(url))
    assert "s3cret-value" in text(admin.get(url))
