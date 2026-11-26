"""Custom storage volumes: create, attach to instances, snapshot, resize."""

from __future__ import annotations

import re

from flask import flash, g, redirect, render_template, request, url_for

from ..incus import IncusError
from .config_forms import check_expiry, check_schedule
from .networks import used_by
from .scoped import bp, run

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
NAME_RULE = "Use letters, digits, dashes, dots and underscores (at most 63)."
SIZE = re.compile(r"\d+(\.\d+)?\s*(B|[KMGTPE]i?B)?")
SIZE_RULE = "Write a size such as 10GiB or 500MB."
CONTENT_TYPES = {"filesystem": "Filesystem", "block": "Block device"}


def _custom(volumes: list) -> list:
    return [v for v in volumes if v.get("type") == "custom"]


def _all_volumes() -> tuple[list, list[str]]:
    rows, problems = [], []
    for pool in g.incus.pools():
        try:
            volumes = _custom(g.incus.volumes(pool["name"]))
        except IncusError as error:
            if error.status == 0:
                raise
            problems.append(f"{pool['name']}: {error.message}")
            continue
        for volume in volumes:
            volume["pool"] = pool["name"]
            volume["instances"] = used_by(volume.get("used_by"))["instances"]
        rows.extend(volumes)
    return sorted(rows, key=lambda v: (v["name"], v["pool"])), problems


def _size(form) -> str:
    size = form.get("size", "").strip().replace(" ", "")
    if size and not SIZE.fullmatch(size):
        raise IncusError(400, SIZE_RULE)
    return size


def _crumbs(pool: str, name: str, *more) -> list:
    items = [("Volumes", url_for("srv.volumes")), (f"{pool} / {name}", None)]
    if not more:
        return items
    items[-1] = (items[-1][0], url_for("srv.volume", pool=pool, name=name))
    return [*items, *more]


@bp.get("/volumes")
def volumes():
    rows, problems = _all_volumes()
    return render_template("volumes/list.html", volumes=rows, problems=problems)


@bp.route("/volumes/new", methods=["GET", "POST"])
def volume_create():
    pools = [p["name"] for p in g.incus.pools()]
    form = request.form
    if request.method == "POST":
        pool = form.get("pool", "")
        name = form.get("name", "").strip()
        content = form.get("content_type", "filesystem")
        try:
            if pool not in pools:
                raise IncusError(400, "Choose a storage pool.")
            if not NAME.fullmatch(name):
                raise IncusError(400, NAME_RULE)
            if content not in CONTENT_TYPES:
                raise IncusError(400, "Choose filesystem or block.")
            size = _size(form)
        except IncusError as error:
            flash(error.message, "error")
        else:
            payload = {
                "name": name,
                "description": form.get("description", "").strip(),
                "content_type": content,
                "config": {"size": size} if size else {},
            }
            if run(lambda: g.incus.create_volume(pool, payload), f"Volume {name} created."):
                return redirect(url_for("srv.volume", pool=pool, name=name))
    return render_template(
        "volumes/new.html",
        pools=pools,
        types=CONTENT_TYPES,
        form=form,
        chosen_pool=form.get("pool") or request.args.get("pool", ""),
    )


@bp.get("/storage/<pool>/volumes/<name>")
def volume(pool, name):
    found = g.incus.volume(pool, name)
    attached = used_by(found.get("used_by"))["instances"]
    # Block and ISO volumes attach to virtual machines only.
    vms_only = found.get("content_type") in ("block", "iso")
    instances = sorted(
        i["name"]
        for i in g.incus.instances()
        if i["name"] not in attached and (i.get("type") == "virtual-machine" or not vms_only)
    )
    return render_template(
        "volumes/detail.html",
        pool=pool,
        volume=found,
        attached=attached,
        instances=instances,
        snapshots=g.incus.volume_snapshots(pool, name),
    )


@bp.route("/storage/<pool>/volumes/<name>/edit", methods=["GET", "POST"])
def volume_edit(pool, name):
    found = g.incus.volume(pool, name)
    form = request.form
    if request.method == "POST":
        try:
            size = _size(form)
            schedule = form.get("schedule", "").strip()
            expiry = form.get("expiry", "").strip()
            if schedule:
                check_schedule(schedule)
            if expiry:
                check_expiry(expiry)
        except IncusError as error:
            flash(error.message, "error")
        else:
            config = dict(found.get("config") or {})
            for key, value in (
                ("size", size),
                ("snapshots.schedule", schedule),
                ("snapshots.expiry", expiry),
            ):
                if value:
                    config[key] = value
                else:
                    config.pop(key, None)
            payload = {"description": form.get("description", "").strip(), "config": config}
            if run(lambda: g.incus.update_volume(pool, name, payload), f"Volume {name} updated."):
                return redirect(url_for("srv.volume", pool=pool, name=name))
    return render_template(
        "volumes/edit.html",
        pool=pool,
        volume=found,
        form=form,
        crumbs=_crumbs(pool, name, ("Edit", None)),
    )


@bp.route("/storage/<pool>/volumes/<name>/rename", methods=["GET", "POST"])
def volume_rename(pool, name):
    form = request.form
    if request.method == "POST":
        new_name = form.get("new_name", "").strip()
        if not NAME.fullmatch(new_name):
            flash(NAME_RULE, "error")
        elif new_name == name:
            return redirect(url_for("srv.volume", pool=pool, name=name))
        elif run(
            lambda: g.incus.rename_volume(pool, name, new_name), f"{name} renamed to {new_name}."
        ):
            return redirect(url_for("srv.volume", pool=pool, name=new_name))
    return render_template(
        "volumes/rename.html",
        pool=pool,
        name=name,
        form=form,
        crumbs=_crumbs(pool, name, ("Rename", None)),
    )


@bp.post("/storage/<pool>/volumes/<name>/delete")
def volume_delete(pool, name):
    if run(lambda: g.incus.delete_volume(pool, name), f"Volume {name} deleted."):
        return redirect(url_for("srv.volumes"))
    return redirect(url_for("srv.volume", pool=pool, name=name))


# -- attaching to instances -------------------------------------------------


def _instance_payload(instance: dict) -> dict:
    keys = ("architecture", "config", "devices", "ephemeral", "profiles", "stateful", "description")
    return {key: instance[key] for key in keys if key in instance}


@bp.post("/storage/<pool>/volumes/<name>/attach")
def volume_attach(pool, name):
    form = request.form
    target = form.get("instance", "")
    mount = form.get("path", "").strip()
    device = form.get("device", "").strip() or name
    back = redirect(url_for("srv.volume", pool=pool, name=name))
    found = g.incus.volume(pool, name)
    is_block = found.get("content_type") in ("block", "iso")
    if not target:
        flash("Choose an instance.", "error")
        return back
    if not is_block and not mount.startswith("/"):
        flash("Give the path to mount it at, such as /data.", "error")
        return back
    try:
        instance = g.incus.instance(target)
    except IncusError as error:
        flash(error.message, "error")
        return back
    devices = dict(instance.get("devices") or {})
    if device in devices or device in (instance.get("expanded_devices") or {}):
        flash(f"{target} already has a device named {device}. Give it another name.", "error")
        return back
    devices[device] = {"type": "disk", "pool": pool, "source": name}
    if not is_block:
        devices[device]["path"] = mount
    payload = {**_instance_payload(instance), "devices": devices}
    run(lambda: g.incus.update_instance(target, payload), f"{name} attached to {target}.")
    return back


@bp.post("/storage/<pool>/volumes/<name>/detach/<instance_name>")
def volume_detach(pool, name, instance_name):
    instance = g.incus.instance(instance_name)
    devices = dict(instance.get("devices") or {})
    matching = [
        key
        for key, device in devices.items()
        if device.get("type") == "disk"
        and device.get("source") == name
        and device.get("pool") == pool
    ]
    if not matching:
        flash(f"{instance_name} gets this volume from a profile; change it there.", "error")
    else:
        for key in matching:
            del devices[key]
        payload = {**_instance_payload(instance), "devices": devices}
        run(
            lambda: g.incus.update_instance(instance_name, payload),
            f"{name} detached from {instance_name}.",
        )
    return redirect(url_for("srv.volume", pool=pool, name=name))


# -- snapshots --------------------------------------------------------------


@bp.post("/storage/<pool>/volumes/<name>/snapshots")
def volume_snapshot_create(pool, name):
    snapshot = request.form.get("snapshot", "").strip()
    if not NAME.fullmatch(snapshot):
        flash(NAME_RULE, "error")
    else:
        run(
            lambda: g.incus.create_volume_snapshot(pool, name, snapshot),
            f"Snapshot {snapshot} created.",
        )
    return redirect(url_for("srv.volume", pool=pool, name=name))


@bp.post("/storage/<pool>/volumes/<name>/snapshots/<snapshot>/restore")
def volume_snapshot_restore(pool, name, snapshot):
    run(
        lambda: g.incus.restore_volume_snapshot(pool, name, snapshot),
        f"{name} restored to {snapshot}.",
    )
    return redirect(url_for("srv.volume", pool=pool, name=name))


@bp.post("/storage/<pool>/volumes/<name>/snapshots/<snapshot>/delete")
def volume_snapshot_delete(pool, name, snapshot):
    run(
        lambda: g.incus.delete_volume_snapshot(pool, name, snapshot),
        f"Snapshot {snapshot} deleted.",
    )
    return redirect(url_for("srv.volume", pool=pool, name=name))
