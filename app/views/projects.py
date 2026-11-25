"""Projects: create, configure (features, limits, restrictions), rename, delete.

The form has three parts: details and features, resource limits, and
restrictions.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from flask import flash, g, redirect, render_template, request, session, url_for

from .. import access, user_certs
from ..db import get_db
from ..incus import IncusError
from .config_forms import apply_settings, dump_yaml, load_yaml, settings_fields
from .scoped import bp, run

DEFAULT = "default"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
NAME_RULE = "Use letters, digits, dashes, dots and underscores (at most 63)."
EDITABLE = ("config", "description")

# (key, label, kind, help) as in config_forms.
FEATURES = [
    ("features.images", "Images", "feature", ""),
    ("features.profiles", "Profiles", "feature", ""),
    ("features.storage.volumes", "Storage volumes", "feature", ""),
    ("features.storage.buckets", "Storage buckets", "feature", ""),
    ("features.networks", "Networks", "feature", ""),
    ("features.networks.zones", "Network zones", "feature", ""),
]
# What Incus uses when a feature is not set.
FEATURE_DEFAULTS = {
    "features.images": "true",
    "features.profiles": "true",
    "features.storage.volumes": "true",
    "features.storage.buckets": "true",
    "features.networks": "false",
    "features.networks.zones": "false",
}
LIMITS = [
    ("limits.instances", "Instances", "text", "The most instances of any kind."),
    ("limits.containers", "Containers", "text", ""),
    ("limits.virtual-machines", "Virtual machines", "text", ""),
    ("limits.cpu", "CPUs", "text", "The total of the instances' limits.cpu."),
    ("limits.memory", "Memory", "text", "The total of the instances' limits.memory, e.g. 16GiB."),
    ("limits.disk", "Disk", "text", "The total size of the project's volumes, e.g. 200GiB."),
    ("limits.processes", "Processes", "text", "The total of the instances' limits.processes."),
    ("limits.networks", "Networks", "text", ""),
]
ALLOW_BLOCK = ("allow", "block")
RESTRICTIONS = [
    (
        "restricted",
        "Apply the restrictions below",
        "bool",
        "When off, nothing below is enforced.",
    ),
    ("restricted.containers.nesting", "Nested containers", "choice", ""),
    ("restricted.containers.privilege", "Container privilege", "choice", ""),
    ("restricted.containers.lowlevel", "Low-level container options", "choice", ""),
    ("restricted.containers.interception", "System call interception", "choice", ""),
    ("restricted.virtual-machines.lowlevel", "Low-level VM options", "choice", ""),
    ("restricted.snapshots", "Snapshots", "choice", ""),
    ("restricted.devices.disk", "Disk devices", "choice", "managed: only the pools' volumes."),
    (
        "restricted.devices.disk.paths",
        "Allowed host paths",
        "text",
        "For disk devices, comma-separated.",
    ),
    ("restricted.devices.nic", "Network devices", "choice", "managed: only managed networks."),
    ("restricted.devices.gpu", "GPU devices", "choice", ""),
    ("restricted.devices.usb", "USB devices", "choice", ""),
    ("restricted.devices.pci", "PCI devices", "choice", ""),
    ("restricted.devices.infiniband", "InfiniBand devices", "choice", ""),
    ("restricted.devices.unix-char", "Unix character devices", "choice", ""),
    ("restricted.devices.unix-block", "Unix block devices", "choice", ""),
    ("restricted.devices.unix-hotplug", "Unix hotplug devices", "choice", ""),
    ("restricted.networks.access", "Networks it may use", "text", "Comma-separated names."),
    ("restricted.networks.uplinks", "Uplink networks", "text", "Comma-separated names."),
    ("restricted.networks.subnets", "Subnets", "text", "Comma-separated <uplink>:<subnet>."),
    ("restricted.idmap.uid", "Host UIDs it may map", "text", "Ranges such as 1000-2000."),
    ("restricted.idmap.gid", "Host GIDs it may map", "text", "Ranges such as 1000-2000."),
]
CHOICES = {
    "restricted.containers.nesting": ALLOW_BLOCK,
    "restricted.containers.privilege": ("unprivileged", "isolated", "allow"),
    "restricted.containers.lowlevel": ALLOW_BLOCK,
    "restricted.containers.interception": ("allow", "block", "full"),
    "restricted.virtual-machines.lowlevel": ALLOW_BLOCK,
    "restricted.snapshots": ALLOW_BLOCK,
    "restricted.devices.disk": ("allow", "managed", "block"),
    "restricted.devices.nic": ("allow", "managed", "block"),
    **{
        f"restricted.devices.{kind}": ALLOW_BLOCK
        for kind in ("gpu", "usb", "pci", "infiniband", "unix-char", "unix-block", "unix-hotplug")
    },
}
# Incus's defaults once "restricted" is on, shown in the choice lists.
RESTRICTION_DEFAULTS = {
    "restricted.containers.nesting": "block",
    "restricted.containers.privilege": "unprivileged",
    "restricted.containers.lowlevel": "block",
    "restricted.containers.interception": "block",
    "restricted.virtual-machines.lowlevel": "block",
    "restricted.snapshots": "allow",
    "restricted.devices.disk": "managed",
    "restricted.devices.nic": "managed",
    **{
        f"restricted.devices.{kind}": "block"
        for kind in ("gpu", "usb", "pci", "infiniband", "unix-char", "unix-block", "unix-hotplug")
    },
    "restricted": "false",
}
SETTINGS = FEATURES + LIMITS + RESTRICTIONS
GROUPS = {
    "features.images": "Features",
    "limits.instances": "Resource limits",
    "restricted": "Restrictions",
}
SECTIONS = (("Features", FEATURES), ("Resource limits", LIMITS), ("Restrictions", RESTRICTIONS))
USAGE = (
    ("instances", "Instances", False),
    ("containers", "Containers", False),
    ("virtual-machines", "Virtual machines", False),
    ("cpu", "CPUs", False),
    ("memory", "Memory", True),
    ("disk", "Disk", True),
    ("processes", "Processes", False),
    ("networks", "Networks", False),
)


def _fields(config: dict) -> list[dict]:
    return settings_fields(
        config,
        settings=SETTINGS,
        empty="No limit",
        groups=GROUPS,
        choices=CHOICES,
        defaults={**FEATURE_DEFAULTS, **RESTRICTION_DEFAULTS},
    )


def _used(project: dict) -> dict[str, int]:
    """How many of each kind of thing a project holds, from its used_by list."""
    counts: dict[str, int] = {}
    for url in project.get("used_by") or []:
        parts = urlsplit(url).path.split("/")
        if len(parts) >= 3:
            kind = unquote(parts[2])
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _crumbs(name: str, last: str | None = None) -> list:
    items = [("Projects", url_for("srv.projects"))]
    if last is None:
        return [*items, (name, None)]
    return [*items, (name, url_for("srv.project_detail", name=name)), (last, None)]


def _resync() -> None:
    """Users' restricted certificates list projects by name; bring them up to date."""
    problems = user_certs.sync_server(g.server)
    if problems:
        flash("Some users' certificates were not updated: " + "; ".join(problems), "error")


def _grants_follow(old: str, new: str | None) -> None:
    """Access given in a renamed project moves with it; in a deleted one it goes."""
    db = get_db()
    if new is None:
        db.execute("DELETE FROM grants WHERE server_id = ? AND project = ?", (g.sid, old))
        db.execute("DELETE FROM group_grants WHERE server_id = ? AND project = ?", (g.sid, old))
    else:
        db.execute(
            "UPDATE OR REPLACE grants SET project = ? WHERE server_id = ? AND project = ?",
            (new, g.sid, old),
        )
        db.execute(
            "UPDATE OR REPLACE group_grants SET project = ? WHERE server_id = ? AND project = ?",
            (new, g.sid, old),
        )
    db.commit()
    _resync()


@bp.get("/projects")
def projects():
    rows = sorted(g.incus.projects(), key=lambda p: (p["name"] != DEFAULT, p["name"]))
    shown = set(access.visible_projects(g.sid, [row["name"] for row in rows]))
    rows = [row for row in rows if row["name"] in shown]
    for row in rows:
        row["counts"] = _used(row)
    return render_template("projects/list.html", rows=rows)


@bp.route("/projects/new", methods=["GET", "POST"])
def project_create():
    form = request.form
    config = {}
    if request.method == "POST":
        name = form.get("name", "").strip()
        description = form.get("description", "").strip()
        try:
            if not NAME.fullmatch(name):
                raise IncusError(400, NAME_RULE)
            config = apply_settings({}, form, SETTINGS, choices=CHOICES)
        except IncusError as error:
            flash(error.message, "error")
            config = apply_settings({}, form, SETTINGS, check=False, choices=CHOICES)
        else:
            payload = {"name": name, "description": description, "config": config}
            if run(lambda: g.incus.create_project(payload), f"Project {name} created."):
                _resync()
                return redirect(url_for("srv.project_detail", name=name))
    return render_template("projects/form.html", form=form, fields=_fields(config), creating=True)


@bp.get("/projects/<name>")
def project_detail(name):
    found = g.incus.project_details(name)
    state = {}
    try:
        state = g.incus.project_state(name)
    except IncusError as error:
        if error.status == 0:
            raise
    resources = (state.get("resources") or {}) if isinstance(state, dict) else {}
    usage = []
    for key, label, is_bytes in USAGE:
        entry = resources.get(key)
        if entry:
            usage.append(
                {
                    "label": label,
                    "usage": entry.get("Usage", entry.get("usage", 0)),
                    "limit": entry.get("Limit", entry.get("limit", -1)),
                    "bytes": is_bytes,
                }
            )
    config = found.get("config") or {}
    defaults = {**FEATURE_DEFAULTS, **RESTRICTION_DEFAULTS}
    sections = []
    for title, settings in SECTIONS:
        rows = []
        for key, label, kind, _help in settings:
            if key == "restricted":
                continue
            value = config.get(key)
            if kind == "feature":
                # The default project always has its own of everything.
                own = name == DEFAULT or (value or defaults[key]) == "true"
                rows.append((label, "Own" if own else "Shared with default"))
            elif value:
                rows.append((label, value))
        sections.append({"title": title, "rows": rows})
    return render_template(
        "projects/detail.html",
        project=found,
        counts=_used(found),
        usage=usage,
        sections=sections,
        is_default=name == DEFAULT,
        is_current=name == g.project,
    )


@bp.route("/projects/<name>/edit", methods=["GET", "POST"])
def project_edit(name):
    found = g.incus.project_details(name)
    form = request.form
    config = found.get("config") or {}
    description = found.get("description") or ""
    if request.method == "POST":
        description = form.get("description", "").strip()
        try:
            config = apply_settings(config, form, SETTINGS, choices=CHOICES)
        except IncusError as error:
            flash(error.message, "error")
            config = apply_settings(config, form, SETTINGS, check=False, choices=CHOICES)
        else:
            payload = {"description": description, "config": config}
            if run(lambda: g.incus.update_project(name, payload), f"Project {name} updated."):
                return redirect(url_for("srv.project_detail", name=name))
    return render_template(
        "projects/form.html",
        form=form,
        fields=_fields(config),
        creating=False,
        name=name,
        description=description,
        crumbs=_crumbs(name, "Configuration"),
    )


@bp.route("/projects/<name>/yaml", methods=["GET", "POST"])
def project_yaml(name):
    if request.method == "POST":
        text = request.form.get("yaml", "")
        try:
            payload = load_yaml(text, EDITABLE)
        except IncusError as error:
            flash(error.message, "error")
        else:
            if run(lambda: g.incus.update_project(name, payload), f"Project {name} updated."):
                return redirect(url_for("srv.project_detail", name=name))
    else:
        text = dump_yaml(g.incus.project_details(name), EDITABLE)
    return render_template(
        "config/yaml.html",
        title=f"Edit {name}",
        crumbs=_crumbs(name, "Edit YAML"),
        back=url_for("srv.project_detail", name=name),
        command=f"incus project edit {name}",
        volatile=False,
        text=text,
    )


@bp.post("/projects/<name>/use")
def project_use(name):
    session[f"project:{g.sid}"] = name
    flash(f"Now working in project {name}.", "success")
    return redirect(url_for("srv.instances"))


@bp.route("/projects/<name>/rename", methods=["GET", "POST"])
def project_rename(name):
    if name == DEFAULT:
        flash("The default project cannot be renamed.", "error")
        return redirect(url_for("srv.project_detail", name=name))
    if request.method == "POST":
        new_name = request.form.get("new_name", "").strip()
        if not NAME.fullmatch(new_name):
            flash(NAME_RULE, "error")
        elif new_name == name:
            return redirect(url_for("srv.project_detail", name=name))
        elif run(lambda: g.incus.rename_project(name, new_name), f"{name} renamed to {new_name}."):
            if session.get(f"project:{g.sid}") == name:
                session[f"project:{g.sid}"] = new_name
            _grants_follow(name, new_name)
            return redirect(url_for("srv.project_detail", name=new_name))
    return render_template(
        "projects/rename.html", name=name, form=request.form, crumbs=_crumbs(name, "Rename")
    )


@bp.post("/projects/<name>/delete")
def project_delete(name):
    if name == DEFAULT:
        flash("The default project cannot be deleted.", "error")
    elif run(lambda: g.incus.delete_project(name), f"Project {name} deleted."):
        if session.get(f"project:{g.sid}") == name:
            session[f"project:{g.sid}"] = DEFAULT
        _grants_follow(name, None)
        return redirect(url_for("srv.projects"))
    return redirect(url_for("srv.project_detail", name=name))
