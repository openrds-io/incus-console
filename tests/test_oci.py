"""OCI application containers: created from a registry image, run by Incus 6.3+."""

import json
import re
import threading

import pytest

from app.views.oci import oci_source

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


def test_private_registry_login_goes_into_the_server_address(browser, sid, fake):
    create(
        browser,
        sid,
        name="app",
        oci_image="ghcr.io/acme/app:1",
        registry_user="bot",
        registry_password="p@ss/w:rd",
    )
    sent = fake.created[-1]["source"]
    assert sent["server"] == "https://bot:p%40ss%2Fw%3Ard@ghcr.io" and sent["alias"] == "acme/app:1"
    # Half a login is a mistake, and the password is never sent back to the page.
    page = text(
        create(browser, sid, name="app2", oci_image="ghcr.io/acme/app:1", registry_user="bot")
    )
    assert "needs both a username and a password" in page
    refused = text(
        create(
            browser,
            sid,
            name="bad name",
            oci_image="nginx",
            registry_user="bot",
            registry_password="s3cret-token",
        )
    )
    assert "s3cret-token" not in refused and 'value="bot"' in refused


def test_a_login_in_an_image_source_is_not_shown(browser, sid, fake):
    fingerprint = fake.add_image("Private app")
    fake.images[fingerprint]["update_source"] = {
        "server": "https://bot:s3cret-token@ghcr.io",
        "alias": "acme/app:1",
        "protocol": "oci",
    }
    page = text(browser.get(f"/servers/{sid}/images/{fingerprint}"))
    assert "https://ghcr.io" in page and "s3cret-token" not in page and "bot:" not in page


def test_ports_and_volumes_become_devices(browser, sid, fake):
    fake.add_volume("default", "pgdata")
    create(
        browser,
        sid,
        name="db",
        oci_image="postgres:18-alpine",
        ports="5432:5432\n8080:80/udp\n9000",
        volumes="pgdata:/var/lib/postgresql\nlogs:/var/log/app",
    )
    devices = fake.created[-1]["devices"]
    assert devices["port-5432"] == {
        "type": "proxy",
        "listen": "tcp:0.0.0.0:5432",
        "connect": "tcp:127.0.0.1:5432",
    }
    assert devices["port-8080-udp"]["connect"] == "udp:127.0.0.1:80"
    assert devices["port-9000"]["listen"] == "tcp:0.0.0.0:9000"
    assert devices["pgdata"] == {
        "type": "disk",
        "pool": "default",
        "source": "pgdata",
        "path": "/var/lib/postgresql",
    }
    # The volume that did not exist was made; the one that did was left alone.
    assert set(fake.volumes["default"]) == {"pgdata", "logs"}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ports", "eighty", "is not a port"),
        ("ports", "70000:80", "ports go from 1 to 65535"),
        ("ports", "80\n80", "listed twice"),
        ("volumes", "data", "is not a volume"),
        ("volumes", "data:relative", "is not a volume"),
        ("volumes", "a:/x\na:/y", "listed twice"),
    ],
)
def test_bad_ports_and_volumes_are_refused(browser, sid, fake, field, value, message):
    page = text(create(browser, sid, name="app", oci_image="nginx", **{field: value}))
    assert message in page and not fake.created and not fake.volumes.get("default")


def test_new_volumes_on_a_cluster_need_a_member(browser, sid, fake):
    fake.make_cluster("node1", "node2")
    page = text(create(browser, sid, name="db", oci_image="redis", volumes="data:/data"))
    assert "Choose the cluster member" in page and not fake.created
    create(browser, sid, name="db", oci_image="redis", volumes="data:/data", target="node2")
    assert fake.volumes["default"]["data"]["location"] == "node2"
    assert fake.instances["db"]["location"] == "node2"


def test_presets_are_offered_to_the_form(browser, sid, fake):
    form = text(browser.get(f"/servers/{sid}/create-instance?type=oci"))
    assert "data-oci-preset" in form and "pgdata:/var/lib/postgresql" in form
    assert "POSTGRES_PASSWORD=change-me" in form and 'name="ports"' in form


def test_update_image_rebuilds_a_stopped_application_container(browser, sid, fake):
    create(browser, sid, name="web", oci_image="nginx:alpine", start="")
    image = fake.add_image("nginx")
    fake.images[image]["update_source"] = {
        "server": "https://docker.io",
        "alias": "nginx:alpine",
        "protocol": "oci",
    }
    fake.instances["web"]["config"]["volatile.base_image"] = image
    assert "Update image" in text(browser.get(f"/servers/{sid}/instances/web"))
    url = f"/servers/{sid}/instances/web/update-image"
    assert 'value="nginx:alpine"' in text(browser.get(url))
    done = browser.post(url, {"oci_image": "nginx:1.29"}, follow_redirects=True)
    assert "web rebuilt on nginx:1.29." in text(done)
    name, body = fake.rebuilt[-1]
    assert name == "web" and body["source"] == {
        "type": "image",
        "mode": "pull",
        "protocol": "oci",
        "server": "https://docker.io",
        "alias": "nginx:1.29",
    }
    assert "is not an image name" in text(browser.post(url, {"oci_image": "Not An Image"}))
    # Running ones are told to stop first; system containers have no such page.
    fake.instances["web"].update(status="Running", status_code=103)
    assert "Stop web first" in text(browser.get(url))
    fake.add_instance("sys")
    assert "Update image" not in text(browser.get(f"/servers/{sid}/instances/sys"))
    refused = browser.post(f"/servers/{sid}/instances/sys/update-image", follow_redirects=True)
    assert "Only application containers" in text(refused) and len(fake.rebuilt) == 1
