from __future__ import annotations

import re
from urllib.parse import urlsplit

import yaml
from flask import current_app, flash, g, redirect, render_template, request, url_for

from .. import terminal
from ..incus import IncusError
from .scoped import bp, run

ACTIONS = {
    "start": "started",
    "stop": "stopped",
    "restart": "restarted",
    "freeze": "frozen",
    "unfreeze": "resumed",
}
# What ``incus config edit`` exposes; sent back whole with PUT.
EDITABLE = ("architecture", "config", "devices", "ephemeral", "profiles", "stateful", "description")


@bp.get("/instances")
def instances():
    rows = sorted(g.incus.instances(), key=lambda i: i["name"])
    return render_template("instances/list.html", instances=rows)


@bp.route("/create-instance", methods=["GET", "POST"])
def instance_create():
    config = current_app.config
    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()
        source = {"type": "image"}
        if form.get("source") == "local":
            source["fingerprint"] = form.get("fingerprint", "")
        else:
            source.update(
                mode="pull",
                protocol="simplestreams",
                server=form.get("image_server", "").strip() or config["DEFAULT_IMAGE_SERVER"],
                alias=form.get("alias", "").strip(),
            )
        limits = {
            key: form.get(field, "").strip()
            for key, field in (("limits.cpu", "cpu"), ("limits.memory", "memory"))
        }
        payload = {
            "name": name,
            "type": "container",
            "description": form.get("description", "").strip(),
            "profiles": form.getlist("profiles") or ["default"],
            "config": {key: value for key, value in limits.items() if value},
            "source": source,
            "start": bool(form.get("start")),
        }
        if not name:
            flash("Give the container a name.", "error")
        elif run(lambda: g.incus.create_instance(payload), f"Container {name} created."):
            return redirect(url_for("srv.instances"))
    return render_template(
        "instances/new.html",
        profiles=[p["name"] for p in g.incus.profiles()],
        images=g.incus.images(),
        image_server=config["DEFAULT_IMAGE_SERVER"],
        suggestions=config["IMAGE_SUGGESTIONS"],
        form=request.form,
    )


@bp.get("/instances/<name>")
def instance(name):
    return render_template("instances/detail.html", instance=g.incus.instance(name))


def _back(name):
    if request.form.get("back") == "list":
        return redirect(url_for("srv.instances"))
    return redirect(url_for("srv.instance", name=name))


@bp.post("/instances/<name>/action")
def instance_action(name):
    action = request.form.get("action", "")
    if action not in ACTIONS:
        flash("Unknown action.", "error")
        return _back(name)
    force = bool(request.form.get("force"))
    run(lambda: g.incus.instance_action(name, action, force), f"{name} {ACTIONS[action]}.")
    return _back(name)


@bp.post("/instances/<name>/delete")
def instance_delete(name):
    if run(lambda: g.incus.delete_instance(name), f"{name} deleted."):
        return redirect(url_for("srv.instances"))
    return redirect(url_for("srv.instance", name=name))


@bp.route("/instances/<name>/edit", methods=["GET", "POST"])
def instance_edit(name):
    if request.method == "POST":
        text = request.form.get("yaml", "")
        try:
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                raise IncusError(400, "The configuration must be a YAML mapping.")
            payload = {key: data[key] for key in EDITABLE if key in data}
        except yaml.YAMLError as error:
            flash(f"Invalid YAML: {error}", "error")
        except IncusError as error:
            flash(error.message, "error")
        else:
            if run(lambda: g.incus.update_instance(name, payload), f"{name} updated."):
                return redirect(url_for("srv.instance", name=name))
    else:
        current = g.incus.instance(name)
        editable = {key: current[key] for key in EDITABLE if key in current}
        text = yaml.safe_dump(editable, sort_keys=False, default_flow_style=False)
    return render_template("instances/edit.html", name=name, text=text)


@bp.post("/instances/<name>/snapshots")
def snapshot_create(name):
    snapshot = request.form.get("snapshot", "").strip()
    if not snapshot:
        flash("Give the snapshot a name.", "error")
    else:
        run(lambda: g.incus.create_snapshot(name, snapshot), f"Snapshot {snapshot} created.")
    return redirect(url_for("srv.instance", name=name))


@bp.post("/instances/<name>/snapshots/<snapshot>/restore")
def snapshot_restore(name, snapshot):
    run(lambda: g.incus.restore_snapshot(name, snapshot), f"{name} restored to {snapshot}.")
    return redirect(url_for("srv.instance", name=name))


@bp.post("/instances/<name>/snapshots/<snapshot>/delete")
def snapshot_delete(name, snapshot):
    run(lambda: g.incus.delete_snapshot(name, snapshot), f"Snapshot {snapshot} deleted.")
    return redirect(url_for("srv.instance", name=name))


@bp.get("/instances/<name>/terminal")
def instance_terminal(name):
    # url_for gives a websocket rule as an absolute ws:// URL; the page wants the path.
    ws_path = urlsplit(url_for("srv.instance_terminal_ws", name=name)).path
    return render_template(
        "instances/terminal.html", instance=g.incus.instance(name), ws_path=ws_path
    )


def instance_terminal_ws(name):
    return terminal.relay(g.incus, name, current_app.config["TERMINAL_COMMAND"])


bp.add_url_rule(
    "/instances/<name>/terminal/ws",
    "instance_terminal_ws",
    instance_terminal_ws,
    websocket=True,
)


# -- rename and copy ------------------------------------------------------

NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,62}")
NAME_RULE = "Use letters, digits and dashes, starting with a letter (at most 63)."


@bp.route("/instances/<name>/rename", methods=["GET", "POST"])
def instance_rename(name):
    instance = g.incus.instance(name)
    if request.method == "POST":
        new_name = request.form.get("new_name", "").strip()
        if not NAME.fullmatch(new_name):
            flash(NAME_RULE, "error")
        elif new_name == name:
            return redirect(url_for("srv.instance", name=name))
        elif run(lambda: g.incus.rename_instance(name, new_name), f"{name} renamed to {new_name}."):
            return redirect(url_for("srv.instance", name=new_name))
    return render_template("instances/rename.html", instance=instance, form=request.form)


@bp.route("/instances/<name>/copy", methods=["GET", "POST"])
def instance_copy(name):
    instance = g.incus.instance(name)
    if request.method == "POST":
        form = request.form
        new_name = form.get("new_name", "").strip()
        if not NAME.fullmatch(new_name):
            flash(NAME_RULE, "error")
        elif run(
            lambda: g.incus.copy_instance(
                name, new_name, snapshots=bool(form.get("snapshots")), start=bool(form.get("start"))
            ),
            f"{name} copied to {new_name}.",
        ):
            return redirect(url_for("srv.instance", name=new_name))
    return render_template("instances/copy.html", instance=instance, form=request.form)


# -- logs -----------------------------------------------------------------

CONSOLE = "console"


@bp.get("/instances/<name>/logs")
def instance_logs(name):
    instance = g.incus.instance(name)
    files = [CONSOLE, *g.incus.instance_log_files(name)]
    current = request.args.get("file", CONSOLE)
    if current not in files:
        current = CONSOLE
    text, problem = "", None
    try:
        if current == CONSOLE:
            text = g.incus.console_log(name)
        else:
            text = g.incus.instance_log(name, current)
    except IncusError as error:
        if error.status == 0:
            raise
        problem = error.message
    return render_template(
        "instances/logs.html",
        instance=instance,
        files=files,
        current=current,
        text=text,
        problem=problem,
    )


# -- settings and devices -------------------------------------------------

# (key, label, kind, help). Empty means "not set here": the value comes from
# the instance's profiles, or Incus's default.
SETTINGS = [
    ("limits.cpu", "CPUs", "text", "A number of CPUs, or CPU ids such as 0-3."),
    ("limits.memory", "Memory", "text", "Such as 2GiB, or 50% of the host."),
    ("limits.processes", "Processes", "text", "The most processes it may run."),
    ("boot.autostart", "Start when the host starts", "bool", ""),
    (
        "security.nesting",
        "Allow nested containers",
        "bool",
        "Needed to run Docker or Incus inside.",
    ),
    (
        "security.privileged",
        "Privileged",
        "bool",
        "Root in the container is root on the host. Avoid unless you need it.",
    ),
]
DEVICE_TYPES = {
    "disk": "source=/srv/data\npath=/data",
    "proxy": "listen=tcp:0.0.0.0:8080\nconnect=tcp:127.0.0.1:80",
    "nic": "network=incusbr0\nname=eth1",
    "unix-char": "source=/dev/fuse\npath=/dev/fuse",
    "unix-block": "source=/dev/sdb\npath=/dev/sdb",
    "usb": "vendorid=0403\nproductid=6001",
    "gpu": "gputype=physical",
}


def _editable(instance: dict) -> dict:
    return {key: instance[key] for key in EDITABLE if key in instance}


def _save(name: str, payload: dict, done: str) -> bool:
    return run(lambda: g.incus.update_instance(name, payload), done)


@bp.route("/instances/<name>/settings", methods=["GET", "POST"])
def instance_settings(name):
    instance = g.incus.instance(name)
    if request.method == "POST":
        payload = _editable(instance)
        config = dict(payload.get("config") or {})
        for key, _label, kind, _help in SETTINGS:
            value = request.form.get(f"cfg:{key}", "").strip()
            if kind == "bool" and value not in ("", "true", "false"):
                value = ""
            if value:
                config[key] = value
            else:
                config.pop(key, None)
        payload["config"] = config
        payload["description"] = request.form.get("description", "").strip()
        if _save(name, payload, f"{name} updated."):
            return redirect(url_for("srv.instance", name=name))
        instance = {**instance, "config": config, "description": payload["description"]}
    local = instance.get("config") or {}
    expanded = instance.get("expanded_config") or {}
    fields = [
        {
            "key": key,
            "label": label,
            "kind": kind,
            "help": help_text,
            "value": local.get(key, ""),
            "inherited": "" if key in local else expanded.get(key, ""),
        }
        for key, label, kind, help_text in SETTINGS
    ]
    return render_template("instances/settings.html", instance=instance, fields=fields)


def _parse_settings(text: str) -> dict:
    settings = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise IncusError(400, f"Line {number}: write each setting as key=value.")
        settings[key.strip()] = value.strip()
    return settings


@bp.route("/instances/<name>/devices/new", methods=["GET", "POST"])
def device_create(name):
    instance = g.incus.instance(name)
    form = request.form
    if request.method == "POST":
        device = form.get("device", "").strip()
        kind = form.get("type", "")
        try:
            if not device:
                raise IncusError(400, "Give the device a name.")
            if kind not in DEVICE_TYPES:
                raise IncusError(400, "Choose a device type.")
            if device in (instance.get("devices") or {}):
                raise IncusError(400, f"{name} already has a device named {device}.")
            settings = _parse_settings(form.get("settings", ""))
        except IncusError as error:
            flash(error.message, "error")
        else:
            payload = _editable(instance)
            devices = dict(payload.get("devices") or {})
            devices[device] = {**settings, "type": kind}
            payload["devices"] = devices
            if _save(name, payload, f"Device {device} added."):
                return redirect(url_for("srv.instance", name=name))
    return render_template(
        "instances/device_new.html", instance=instance, types=DEVICE_TYPES, form=form
    )


@bp.post("/instances/<name>/devices/<device>/delete")
def device_delete(name, device):
    instance = g.incus.instance(name)
    devices = dict(instance.get("devices") or {})
    if device not in devices:
        flash(f"{device} comes from a profile; change it in the profile.", "error")
    else:
        del devices[device]
        payload = {**_editable(instance), "devices": devices}
        _save(name, payload, f"Device {device} removed.")
    return redirect(url_for("srv.instance", name=name))
