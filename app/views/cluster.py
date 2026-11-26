"""Clusters: the members of the cluster a server belongs to, and their groups."""

from __future__ import annotations

import re

from flask import abort, flash, g, redirect, render_template, request, url_for

from ..incus import IncusError
from .scoped import bp, cluster_groups, cluster_members, run

# How an evacuation treats the member's instances. Containers cannot move while
# running, so "migrate" stops each one, moves it and starts it again.
EVACUATE_MODES = {
    "": "As each instance is set (cluster.evacuate)",
    "migrate": "Move them to other members",
    "stop": "Stop them and leave them here",
}
# Roles an admin gives; Incus hands out the database roles itself.
ASSIGNABLE_ROLES = {
    "event-hub": "Relays events between members",
    "ovn-chassis": "Uplink for OVN networks",
}
DEFAULT_GROUP = "default"
GROUP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
GROUP_RULE = "Use letters, digits, dashes, dots and underscores (at most 63)."


def _counts() -> dict[str, int]:
    """Instances of the current project on each member."""
    counts: dict[str, int] = {}
    for instance in g.incus.instances():
        location = instance.get("location") or ""
        counts[location] = counts.get(location, 0) + 1
    return counts


@bp.get("/cluster")
def cluster():
    members = cluster_members()
    return render_template(
        "cluster/members.html",
        members=members,
        groups=cluster_groups(),
        counts=_counts() if members else {},
    )


def _member(name: str) -> dict:
    found = next((m for m in cluster_members() if m["server_name"] == name), None)
    if found is None:
        abort(404)
    return found


# -- one member -------------------------------------------------------------


def _resources(name: str) -> dict:
    """What the member reports about itself, flattened for the page; {} if it cannot say."""
    try:
        state = g.incus.cluster_member_resources(name)
    except IncusError as error:
        if error.status == 0:
            raise
        return {}
    info = state.get("sysinfo") or {}
    pools = [
        {"name": pool, **(usage.get("space") or {})}
        for pool, usage in sorted((state.get("storage_pools") or {}).items())
    ]
    return {**info, "pools": pools} if info or pools else {}


@bp.get("/cluster/members/<member>")
def cluster_member(member):
    found = _member(member)
    instances = sorted(
        i["name"] for i in g.incus.instances() if (i.get("location") or "") == member
    )
    return render_template(
        "cluster/member.html",
        member=found,
        resources=_resources(member) if found.get("status") != "Offline" else {},
        instances=instances,
        modes=EVACUATE_MODES,
        alone=len(cluster_members()) < 2,
        console_address=_is_console_address(found),
    )


@bp.route("/cluster/members/<member>/edit", methods=["GET", "POST"])
def cluster_member_edit(member):
    found = _member(member)
    groups = [group["name"] for group in cluster_groups()]
    if request.method == "POST":
        form = request.form
        chosen = [name for name in form.getlist("groups") if name in groups]
        automatic = [r for r in found.get("roles") or [] if r not in ASSIGNABLE_ROLES]
        payload = {
            "description": form.get("description", "").strip(),
            "failure_domain": form.get("failure_domain", "").strip() or "default",
            "roles": automatic + [r for r in form.getlist("roles") if r in ASSIGNABLE_ROLES],
            "groups": chosen,
            "config": found.get("config") or {},
        }
        if not chosen:
            flash("A member belongs to at least one group.", "error")
        elif run(lambda: g.incus.update_cluster_member(member, payload), f"{member} updated."):
            return redirect(url_for("srv.cluster_member", member=member))
        found = {**found, **payload}
    return render_template(
        "cluster/member_edit.html", member=found, groups=groups, roles=ASSIGNABLE_ROLES
    )


@bp.post("/cluster/members/<member>/evacuate")
def cluster_evacuate(member):
    _member(member)
    mode = request.form.get("mode", "")
    if mode not in EVACUATE_MODES:
        flash("Choose what happens to the instances.", "error")
    else:
        run(
            lambda: g.incus.cluster_member_state(member, "evacuate", mode),
            f"{member} evacuated.",
        )
    return redirect(url_for("srv.cluster_member", member=member))


@bp.post("/cluster/members/<member>/restore")
def cluster_restore(member):
    _member(member)
    run(lambda: g.incus.cluster_member_state(member, "restore"), f"{member} restored.")
    return redirect(url_for("srv.cluster_member", member=member))


def _is_console_address(member: dict) -> bool:
    """Is this the member the console itself connects to?"""
    return g.server["kind"] == "https" and (member.get("url") or "").rstrip("/") == g.server[
        "address"
    ].rstrip("/")


@bp.post("/cluster/members/<member>/remove")
def cluster_member_remove(member):
    found = _member(member)
    if _is_console_address(found):
        flash(
            f"The console reaches the cluster through {member}. Register another member's "
            "address under Servers first.",
            "error",
        )
        return redirect(url_for("srv.cluster_member", member=member))
    if run(lambda: g.incus.remove_cluster_member(member), f"{member} removed from the cluster."):
        return redirect(url_for("srv.cluster"))
    return redirect(url_for("srv.cluster_member", member=member))


# -- groups -----------------------------------------------------------------


def _group(name: str) -> dict:
    found = next((x for x in cluster_groups() if x["name"] == name), None)
    if found is None:
        abort(404)
    return found


def _chosen_members(form) -> list[str]:
    known = {m["server_name"] for m in cluster_members()}
    return [name for name in form.getlist("members") if name in known]


@bp.post("/cluster/groups")
def cluster_group_create():
    if not cluster_members():
        abort(404)
    name = request.form.get("name", "").strip()
    payload = {
        "name": name,
        "description": request.form.get("description", "").strip(),
        "members": [],
    }
    if not GROUP_NAME.fullmatch(name):
        flash(GROUP_RULE, "error")
    elif run(lambda: g.incus.create_cluster_group(payload), f"Group {name} created."):
        return redirect(url_for("srv.cluster_group_edit", name=name))
    return redirect(url_for("srv.cluster"))


@bp.route("/cluster/groups/<name>", methods=["GET", "POST"])
def cluster_group_edit(name):
    group = _group(name)
    if request.method == "POST":
        form = request.form
        new_name = form.get("name", name).strip()
        payload = {
            "description": form.get("description", "").strip(),
            "members": _chosen_members(form),
            "config": group.get("config") or {},
        }
        if not GROUP_NAME.fullmatch(new_name):
            flash(GROUP_RULE, "error")
        elif new_name != name and name == DEFAULT_GROUP:
            flash("The default group keeps its name.", "error")
        elif run(lambda: _save_group(name, new_name, payload), f"Group {new_name} updated."):
            return redirect(url_for("srv.cluster"))
        group = {**group, **payload}
    return render_template(
        "cluster/group.html",
        group=group,
        members=cluster_members(),
        default=name == DEFAULT_GROUP,
    )


def _save_group(name: str, new_name: str, payload: dict) -> dict:
    response = g.incus.update_cluster_group(name, payload)
    if new_name != name:
        response = g.incus.rename_cluster_group(name, new_name)
    return response


@bp.post("/cluster/groups/<name>/delete")
def cluster_group_delete(name):
    _group(name)
    if name == DEFAULT_GROUP:
        flash("The default group cannot be deleted.", "error")
    else:
        run(lambda: g.incus.delete_cluster_group(name), f"Group {name} deleted.")
    return redirect(url_for("srv.cluster"))
