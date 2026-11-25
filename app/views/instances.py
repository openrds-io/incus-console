from __future__ import annotations

import re
from urllib.parse import urlsplit

from flask import current_app, flash, g, redirect, render_template, request, url_for

from .. import catalog, terminal
from ..incus import IncusError
from .config_forms import (
    DEVICE_TYPES,
    apply_settings,
    dump_yaml,
    load_yaml,
    new_device,
    settings_fields,
)
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


def _catalog() -> tuple[list, str | None]:
    """The image server's container images for this host, or a reason there are none."""
    if not current_app.config["IMAGE_CATALOG"]:
        return [], None
    environment = g.incus.server_info().get("environment") or {}
    try:
        groups = catalog.for_server(
            current_app.config["DEFAULT_IMAGE_SERVER"], environment.get("architectures") or []
        )
    except Exception as error:  # noqa: BLE001 - offline, blocked, or a bad index
        current_app.logger.warning("image catalog unavailable: %s", error)
        return [], "The image server's catalog could not be loaded. Type an alias instead."
    return groups, None


@bp.route("/create-instance", methods=["GET", "POST"])
def instance_create():
    config = current_app.config
    groups, catalog_problem = _catalog()
    form = request.form
    if request.method == "POST":
        name = form.get("name", "").strip()
        source = {"type": "image"}
        chosen = form.get("source", "catalog")
        if chosen == "local":
            source["fingerprint"] = form.get("fingerprint", "")
        else:
            alias = form.get("catalog_alias" if chosen == "catalog" else "alias", "").strip()
            server = config["DEFAULT_IMAGE_SERVER"]
            if chosen == "remote":
                server = form.get("image_server", "").strip() or server
            source.update(mode="pull", protocol="simplestreams", server=server, alias=alias)
        settings = {
            "limits.cpu": form.get("cpu", "").strip(),
            "limits.memory": form.get("memory", "").strip(),
            "cloud-init.user-data": form.get("user_data", "").strip(),
        }
        payload = {
            "name": name,
            "type": "container",
            "description": form.get("description", "").strip(),
            "profiles": form.getlist("profiles") or ["default"],
            "config": {key: value for key, value in settings.items() if value},
            "source": source,
            "start": bool(form.get("start")),
        }
        pool = form.get("pool", "").strip()
        if pool:
            payload["devices"] = {"root": {"type": "disk", "path": "/", "pool": pool}}
        if not NAME.fullmatch(name):
            flash(NAME_RULE, "error")
        elif chosen != "local" and not source["alias"]:
            flash("Choose an image.", "error")
        elif run(lambda: g.incus.create_instance(payload), f"Container {name} created."):
            return redirect(url_for("srv.instance", name=name))
    image = request.args.get("image", "")
    default_source = "local" if image else ("catalog" if groups else "remote")
    return render_template(
        "instances/new.html",
        profiles=[p["name"] for p in g.incus.profiles()],
        pools=[p["name"] for p in g.incus.pools()],
        images=g.incus.images(),
        groups=groups,
        catalog_problem=catalog_problem,
        image_server=config["DEFAULT_IMAGE_SERVER"],
        suggestions=config["IMAGE_SUGGESTIONS"],
        default_source=default_source,
        default_alias=config["IMAGE_SUGGESTIONS"][0],
        default_image=image,
        form=form,
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
            payload = load_yaml(text, EDITABLE)
        except IncusError as error:
            flash(error.message, "error")
        else:
            if run(lambda: g.incus.update_instance(name, payload), f"{name} updated."):
                return redirect(url_for("srv.instance", name=name))
    else:
        text = dump_yaml(g.incus.instance(name), EDITABLE)
    return render_template(
        "config/yaml.html",
        title=f"Edit {name}",
        crumbs=_crumbs({"name": name}, "Edit YAML"),
        back=url_for("srv.instance", name=name),
        command=f"incus config edit {name}",
        volatile=True,
        text=text,
    )


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


def _editable(instance: dict) -> dict:
    return {key: instance[key] for key in EDITABLE if key in instance}


def _save(name: str, payload: dict, done: str) -> bool:
    return run(lambda: g.incus.update_instance(name, payload), done)


def _crumbs(instance: dict, last: str) -> list:
    return [
        ("Instances", url_for("srv.instances")),
        (instance["name"], url_for("srv.instance", name=instance["name"])),
        (last, None),
    ]


@bp.route("/instances/<name>/settings", methods=["GET", "POST"])
def instance_settings(name):
    instance = g.incus.instance(name)
    if request.method == "POST":
        payload = _editable(instance)
        payload["config"] = apply_settings(payload.get("config"), request.form)
        payload["description"] = request.form.get("description", "").strip()
        if _save(name, payload, f"{name} updated."):
            return redirect(url_for("srv.instance", name=name))
        instance = {**instance, **payload}
    return render_template(
        "config/settings.html",
        title=f"Settings - {name}",
        crumbs=_crumbs(instance, "Settings"),
        back=url_for("srv.instance", name=name),
        yaml_href=url_for("srv.instance_edit", name=name),
        description=instance.get("description") or "",
        fields=settings_fields(instance.get("config"), instance.get("expanded_config")),
        inherits=True,
    )


@bp.route("/instances/<name>/devices/new", methods=["GET", "POST"])
def device_create(name):
    instance = g.incus.instance(name)
    if request.method == "POST":
        try:
            device, settings = new_device(request.form, name, instance.get("devices"))
        except IncusError as error:
            flash(error.message, "error")
        else:
            payload = _editable(instance)
            payload["devices"] = {**(payload.get("devices") or {}), device: settings}
            if _save(name, payload, f"Device {device} added."):
                return redirect(url_for("srv.instance", name=name))
    return render_template(
        "config/device_new.html",
        title=f"Add device - {name}",
        crumbs=_crumbs(instance, "Add device"),
        back=url_for("srv.instance", name=name),
        types=DEVICE_TYPES,
        form=request.form,
        inherits=True,
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
