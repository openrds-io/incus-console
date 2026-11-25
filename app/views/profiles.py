"""Profiles: the shared configuration and devices instances are built from."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from flask import flash, g, redirect, render_template, request, url_for

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

# What ``incus profile edit`` exposes; sent back whole with PUT.
EDITABLE = ("config", "description", "devices")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
NAME_RULE = "Use letters, digits, dashes, dots and underscores (at most 63)."
DEFAULT = "default"


def instance_names(used_by: list | None) -> list[str]:
    """The instances in a used_by list (it also holds other kinds of things)."""
    names = []
    for url in used_by or []:
        parts = urlsplit(url).path.split("/")
        if len(parts) == 4 and parts[2] == "instances":
            names.append(unquote(parts[3]))
    return sorted(names)


def _editable(profile: dict) -> dict:
    return {key: profile[key] for key in EDITABLE if key in profile}


def _save(name: str, payload: dict, done: str) -> bool:
    return run(lambda: g.incus.update_profile(name, payload), done)


def _crumbs(name: str, last: str | None = None) -> list:
    items = [("Profiles", url_for("srv.profiles"))]
    if last is None:
        return [*items, (name, None)]
    return [*items, (name, url_for("srv.profile", name=name)), (last, None)]


@bp.get("/profiles")
def profiles():
    rows = sorted(g.incus.profiles(), key=lambda p: (p["name"] != DEFAULT, p["name"]))
    for row in rows:
        row["instances"] = instance_names(row.get("used_by"))
    return render_template("profiles/list.html", profiles=rows)


@bp.route("/profiles/new", methods=["GET", "POST"])
def profile_create():
    existing = sorted(p["name"] for p in g.incus.profiles())
    form = request.form
    source = form.get("copy_from") or request.args.get("copy_from", "")
    if request.method == "POST":
        name = form.get("name", "").strip()
        payload = {"name": name, "description": form.get("description", "").strip()}
        if not NAME.fullmatch(name):
            flash(NAME_RULE, "error")
        else:
            try:
                if source:
                    original = g.incus.profile(source)
                    payload["config"] = original.get("config") or {}
                    payload["devices"] = original.get("devices") or {}
            except IncusError as error:
                flash(error.message, "error")
            else:
                if run(lambda: g.incus.create_profile(payload), f"Profile {name} created."):
                    return redirect(url_for("srv.profile", name=name))
    return render_template("profiles/new.html", existing=existing, source=source, form=form)


@bp.get("/profiles/<name>")
def profile(name):
    found = g.incus.profile(name)
    return render_template(
        "profiles/detail.html",
        profile=found,
        instances=instance_names(found.get("used_by")),
        is_default=name == DEFAULT,
    )


@bp.route("/profiles/<name>/settings", methods=["GET", "POST"])
def profile_settings(name):
    found = g.incus.profile(name)
    if request.method == "POST":
        payload = _editable(found)
        payload["config"] = apply_settings(payload.get("config"), request.form)
        payload["description"] = request.form.get("description", "").strip()
        if _save(name, payload, f"Profile {name} updated."):
            return redirect(url_for("srv.profile", name=name))
        found = {**found, **payload}
    return render_template(
        "config/settings.html",
        title=f"Settings - {name}",
        crumbs=_crumbs(name, "Settings"),
        back=url_for("srv.profile", name=name),
        yaml_href=url_for("srv.profile_edit", name=name),
        description=found.get("description") or "",
        fields=settings_fields(found.get("config")),
        inherits=False,
    )


@bp.route("/profiles/<name>/edit", methods=["GET", "POST"])
def profile_edit(name):
    if request.method == "POST":
        text = request.form.get("yaml", "")
        try:
            payload = load_yaml(text, EDITABLE)
        except IncusError as error:
            flash(error.message, "error")
        else:
            if _save(name, payload, f"Profile {name} updated."):
                return redirect(url_for("srv.profile", name=name))
    else:
        text = dump_yaml(g.incus.profile(name), EDITABLE)
    return render_template(
        "config/yaml.html",
        title=f"Edit {name}",
        crumbs=_crumbs(name, "Edit YAML"),
        back=url_for("srv.profile", name=name),
        command=f"incus profile edit {name}",
        volatile=False,
        text=text,
    )


@bp.route("/profiles/<name>/devices/new", methods=["GET", "POST"])
def profile_device_create(name):
    found = g.incus.profile(name)
    if request.method == "POST":
        try:
            device, settings = new_device(request.form, name, found.get("devices"))
        except IncusError as error:
            flash(error.message, "error")
        else:
            payload = _editable(found)
            payload["devices"] = {**(payload.get("devices") or {}), device: settings}
            if _save(name, payload, f"Device {device} added."):
                return redirect(url_for("srv.profile", name=name))
    return render_template(
        "config/device_new.html",
        title=f"Add device - {name}",
        crumbs=_crumbs(name, "Add device"),
        back=url_for("srv.profile", name=name),
        types=DEVICE_TYPES,
        form=request.form,
        inherits=False,
    )


@bp.post("/profiles/<name>/devices/<device>/delete")
def profile_device_delete(name, device):
    found = g.incus.profile(name)
    devices = dict(found.get("devices") or {})
    if device in devices:
        del devices[device]
        _save(name, {**_editable(found), "devices": devices}, f"Device {device} removed.")
    else:
        flash(f"Profile {name} has no device named {device}.", "error")
    return redirect(url_for("srv.profile", name=name))


@bp.route("/profiles/<name>/rename", methods=["GET", "POST"])
def profile_rename(name):
    if name == DEFAULT:
        flash("The default profile cannot be renamed.", "error")
        return redirect(url_for("srv.profile", name=name))
    if request.method == "POST":
        new_name = request.form.get("new_name", "").strip()
        if not NAME.fullmatch(new_name):
            flash(NAME_RULE, "error")
        elif new_name == name:
            return redirect(url_for("srv.profile", name=name))
        elif run(lambda: g.incus.rename_profile(name, new_name), f"{name} renamed to {new_name}."):
            return redirect(url_for("srv.profile", name=new_name))
    return render_template("profiles/rename.html", name=name, form=request.form)


@bp.post("/profiles/<name>/delete")
def profile_delete(name):
    if name == DEFAULT:
        flash("The default profile cannot be deleted.", "error")
    elif run(lambda: g.incus.delete_profile(name), f"Profile {name} deleted."):
        return redirect(url_for("srv.profiles"))
    return redirect(url_for("srv.profile", name=name))
