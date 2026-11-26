from __future__ import annotations

import re
from urllib.parse import urlsplit

from flask import abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import catalog, terminal
from ..filters import instance_kind
from ..incus import IncusError
from .config_forms import (
    DEVICE_TYPES,
    GROUPS,
    OCI_SETTINGS,
    SETTINGS,
    apply_environment,
    apply_settings,
    dump_yaml,
    environment_text,
    load_yaml,
    new_device,
    parse_environment,
    settings_fields,
)
from .scoped import (
    DONE,
    RUNNING,
    bp,
    cluster_members,
    online_members,
    placement_groups,
    run,
    start,
)

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


def _placement(form) -> tuple[str | None, str | None]:
    """(where in the cluster to create it, problem). Nowhere lets Incus choose.

    A member's name, or ``@group`` for any member of a cluster group.
    """
    target = form.get("target", "").strip()
    if not target:
        return None, None
    if target.startswith("@"):
        if target[1:] not in placement_groups():
            return None, f"{target[1:]} is not a cluster group with a member online."
        return target, None
    if target not in online_members():
        return None, f"{target} is not a cluster member that is online."
    return target, None


def _catalog() -> tuple[list, str | None]:
    """The image server's container images for this host, or why there are none."""
    if not current_app.config["IMAGE_CATALOG"]:
        return [], None
    environment = g.incus.server_info().get("environment") or {}
    try:
        groups = catalog.for_server(
            current_app.config["DEFAULT_IMAGE_SERVER"],
            environment.get("architectures") or [],
        )
    except Exception as error:  # noqa: BLE001 - offline, blocked, or a bad index
        current_app.logger.warning("image catalog unavailable: %s", error)
        return [], "The image server's catalog could not be loaded. Type an alias instead."
    return groups, None


KINDS = ("container", "oci")
OCI_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[a-f0-9]{64})?")


def supports_oci() -> bool:
    return "instance_oci" in (g.incus.server_info().get("api_extensions") or [])


def oci_source(reference: str, default_registry: str) -> tuple[str, str]:
    """(registry URL, image) from ``nginx:alpine`` or ``ghcr.io/owner/app:tag``."""
    reference = reference.strip().removeprefix("docker://").removeprefix("https://")
    first, slash, rest = reference.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        return f"https://{first}", rest
    return default_registry.rstrip("/"), reference


def _is_oci_image(image: dict) -> bool:
    return (image.get("properties") or {}).get("type") == "oci"


def image_kind(image: dict) -> str:
    """Which create form an image belongs to: container or oci."""
    return "oci" if _is_oci_image(image) else "container"


@bp.route("/create-instance", methods=["GET", "POST"])
def instance_create():
    config = current_app.config
    form = request.form
    images = g.incus.images()
    image = request.args.get("image", "")
    picked = next((i for i in images if i["fingerprint"] == image), None)
    kind = form.get("kind") or request.args.get("type", "")
    if kind not in KINDS:
        kind = image_kind(picked) if picked else "container"
    oci_ok = supports_oci()
    if kind == "oci":
        groups, catalog_problem = [], None
    else:
        groups, catalog_problem = _catalog()
    if request.method == "POST":
        name = form.get("name", "").strip()
        chosen = form.get("source", "catalog" if kind == "container" else "remote")
        source = {"type": "image"}
        problem = None
        settings = {
            "limits.cpu": form.get("cpu", "").strip(),
            "limits.memory": form.get("memory", "").strip(),
        }
        if kind == "oci":
            if not oci_ok:
                problem = "This server cannot run application containers (Incus 6.3 or later)."
            elif chosen == "local":
                source["fingerprint"] = form.get("fingerprint", "")
            else:
                server, alias = oci_source(form.get("oci_image", ""), config["OCI_REGISTRY"])
                if not alias:
                    problem = "Choose an image."
                elif not OCI_IMAGE.fullmatch(alias):
                    problem = f"{alias!r} is not an image name, such as nginx:alpine."
                source.update(mode="pull", protocol="oci", server=server, alias=alias)
            try:
                settings.update(parse_environment(form.get("environment", "")))
            except IncusError as error:
                problem = problem or error.message
        else:  # a system container, from a system image
            settings["cloud-init.user-data"] = form.get("user_data", "").strip()
            if chosen == "local":
                source["fingerprint"] = form.get("fingerprint", "")
            else:
                alias = form.get("catalog_alias" if chosen == "catalog" else "alias", "").strip()
                server = config["DEFAULT_IMAGE_SERVER"]
                if chosen == "remote":
                    server = form.get("image_server", "").strip() or server
                source.update(mode="pull", protocol="simplestreams", server=server, alias=alias)
                if not alias:
                    problem = "Choose an image."
        payload = {
            "name": name,
            "type": "container",
            "description": form.get("description", "").strip(),
            "profiles": form.getlist("profiles") or ["default"],
            "config": {key: value for key, value in settings.items() if value},
            "source": source,
            "start": bool(form.get("start")),
        }
        target, misplaced = _placement(form)
        problem = problem or misplaced
        pool = form.get("pool", "").strip()
        if pool:
            payload["devices"] = {"root": {"type": "disk", "path": "/", "pool": pool}}
        if not NAME.fullmatch(name):
            flash(NAME_RULE, "error")
        elif problem:
            flash(problem, "error")
        else:
            # Incus makes the instance only after fetching the image, so while that
            # is still running there is no instance page to show yet.
            what = "Application container" if kind == "oci" else "Container"
            outcome = start(
                lambda: g.incus.create_instance(payload, target=target), f"{what} {name} created."
            )
            if outcome == DONE:
                return redirect(url_for("srv.instance", name=name))
            if outcome == RUNNING:
                return redirect(url_for("srv.operations"))
    local = [i for i in images if image_kind(i) == kind]
    default_source = "local" if picked else ("catalog" if groups else "remote")
    return render_template(
        "instances/new.html",
        kind=kind,
        oci_ok=oci_ok,
        profiles=[p["name"] for p in g.incus.profiles()],
        pools=[p["name"] for p in g.incus.pools()],
        images=local,
        groups=groups,
        catalog_problem=catalog_problem,
        image_server=config["DEFAULT_IMAGE_SERVER"],
        suggestions=config["IMAGE_SUGGESTIONS"],
        oci_registry=config["OCI_REGISTRY"],
        oci_suggestions=config["OCI_SUGGESTIONS"],
        default_source=default_source,
        default_alias=config["IMAGE_SUGGESTIONS"][0],
        default_image=image,
        members=online_members(),
        groups_of_members=placement_groups(),
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
    try:
        oci = instance_kind(g.incus.instance(name)) == "oci"
    except IncusError:
        oci = False  # the relay tells the browser what is wrong
    command = current_app.config["TERMINAL_COMMAND_OCI" if oci else "TERMINAL_COMMAND"]
    return terminal.relay(g.incus, name, command)


bp.add_url_rule(
    "/instances/<name>/terminal/ws",
    "instance_terminal_ws",
    instance_terminal_ws,
    websocket=True,
)


# -- consoles ---------------------------------------------------------------


@bp.get("/instances/<name>/console")
def instance_console(name):
    """The text console: boot messages and a login prompt."""
    ws_path = urlsplit(url_for("srv.instance_console_ws", name=name)).path
    return render_template(
        "instances/terminal.html",
        instance=g.incus.instance(name),
        ws_path=ws_path,
        what="Console",
    )


def instance_console_ws(name):
    return terminal.relay(g.incus, name, None)


bp.add_url_rule(
    "/instances/<name>/console/ws",
    "instance_console_ws",
    instance_console_ws,
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


@bp.route("/instances/<name>/move", methods=["GET", "POST"])
def instance_move(name):
    """Move a stopped instance to another member of the cluster."""
    if not cluster_members():
        abort(404)
    instance = g.incus.instance(name)
    here = instance.get("location") or ""
    members = [m for m in online_members() if m != here]
    if request.method == "POST":
        target = request.form.get("target", "")
        if target not in members:
            flash("Choose a cluster member that is online.", "error")
        else:
            outcome = start(
                lambda: g.incus.move_instance(name, target), f"{name} moved to {target}."
            )
            if outcome == DONE:
                return redirect(url_for("srv.instance", name=name))
            if outcome == RUNNING:
                return redirect(url_for("srv.operations"))
    return render_template("instances/move.html", instance=instance, members=members)


@bp.route("/instances/<name>/copy", methods=["GET", "POST"])
def instance_copy(name):
    instance = g.incus.instance(name)
    if request.method == "POST":
        form = request.form
        new_name = form.get("new_name", "").strip()
        target, misplaced = _placement(form)
        if not NAME.fullmatch(new_name):
            flash(NAME_RULE, "error")
        elif misplaced:
            flash(misplaced, "error")
        else:
            outcome = start(
                lambda: g.incus.copy_instance(
                    name,
                    new_name,
                    snapshots=bool(form.get("snapshots")),
                    start=bool(form.get("start")),
                    target=target,
                ),
                f"{name} copied to {new_name}.",
            )
            if outcome == DONE:
                return redirect(url_for("srv.instance", name=new_name))
            if outcome == RUNNING:
                return redirect(url_for("srv.operations"))
    return render_template(
        "instances/copy.html",
        instance=instance,
        members=online_members(),
        groups_of_members=placement_groups(),
        form=request.form,
    )


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
    settings = OCI_SETTINGS + SETTINGS if instance_kind(instance) == "oci" else SETTINGS
    environment = environment_text(instance.get("config"))
    if request.method == "POST":
        payload = _editable(instance)
        payload["description"] = request.form.get("description", "").strip()
        environment = request.form.get("environment", "")
        try:
            config = apply_settings(payload.get("config"), request.form, settings)
            payload["config"] = apply_environment(config, environment)
        except IncusError as error:
            flash(error.message, "error")
            payload["config"] = apply_settings(
                payload.get("config"), request.form, settings, check=False
            )
        else:
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
        fields=settings_fields(
            instance.get("config"),
            instance.get("expanded_config"),
            settings,
            groups=GROUPS,
        ),
        environment=environment,
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
