from __future__ import annotations

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
