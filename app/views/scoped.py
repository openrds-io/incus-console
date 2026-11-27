"""Pages that belong to one registered server live under /servers/<sid>/."""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, g, request, session

from .. import access, servers, tracker, user_certs
from ..db import get_db
from ..incus import IncusError

bp = Blueprint("srv", __name__, url_prefix="/servers/<int:sid>")


@bp.url_value_preprocessor
def _pull_server_id(_endpoint, values):
    g.sid = values.pop("sid")


@bp.before_request
def _load_server():
    server = servers.get(g.sid)
    if server is None:
        abort(404)
    g.server = server
    if not access.visible_servers([server]):
        abort(404)
    key = f"project:{g.sid}"
    if request.args.get("project"):
        session[key] = request.args["project"]
    g.project = session.get(key, "default")
    if not access.enters(g.sid, g.project):
        # Land users who cannot see the remembered project in one they can.
        granted = access.granted_projects(g.sid)
        if not granted:
            abort(403)
        g.project = session[key] = granted[0]
    _authorize()
    g.incus = user_certs.client(server, g.user, g.project) or servers.client_for(server, g.project)
    if request.method == "GET":
        for row in tracker.unseen(get_db(), g.sid, g.project):
            if row["status"] == "Success":
                flash(row["label"], "success")
            else:
                outcome = row["error"] or row["status"].lower()
                flash(f"Did not finish: {_undone(row['label'])} ({outcome})", "error")


def _authorize() -> None:
    need = access.required(request.endpoint or "")
    # Project pages act on the project in the address, not the one being worked in.
    target = (
        (request.view_args or {}).get("name")
        if request.endpoint.startswith("srv.project_")
        else None
    )
    if access.can(need, project=target):
        return
    # No such role in the project: perhaps on the one instance this page is about.
    endpoint = request.endpoint or ""
    name = (request.view_args or {}).get("name")
    if need in ("view", "operate") and name and access.is_instance_page(endpoint):
        g.instance_name = name
        if access.can(need, instance=name):
            return
    # The list (cut down to their own) and its live updates are theirs to see.
    if endpoint in ("srv.instances", "srv.events") and access.enters(g.sid, g.project):
        return
    abort(403)


def cluster_members() -> list[dict]:
    """The members of the cluster this server belongs to; [] when it stands alone."""
    if "cluster_members" in g:
        return g.cluster_members
    g.cluster_members = []
    if g.get("incus_failed"):
        return g.cluster_members
    try:
        environment = g.incus.server_info().get("environment") or {}
        if environment.get("server_clustered"):
            g.cluster_members = sorted(_members(), key=lambda m: m["server_name"])
    except IncusError:
        pass
    return g.cluster_members


def _as_console(read):
    """``read(client)`` as the user, or as the console where Incus refuses the user.

    A certificate restricted to projects may not be shown the cluster's members
    and groups. Their names are no secret to someone placing an instance.
    """
    try:
        return read(g.incus)
    except IncusError as error:
        if error.status != 403:
            raise
    return read(servers.client_for(g.server, g.project))


def _members() -> list[dict]:
    return _as_console(lambda client: client.cluster_members())


def cluster_groups() -> list[dict]:
    """The cluster's groups of members; [] on a server that stands alone."""
    if not cluster_members():
        return []
    if "cluster_groups" not in g:
        found = _as_console(lambda client: client.cluster_groups())
        g.cluster_groups = sorted(found, key=lambda group: group["name"])
    return g.cluster_groups


def placement_groups() -> list[str]:
    """Names of the groups with a member that can take an instance now."""
    online = set(online_members())
    return [x["name"] for x in cluster_groups() if online & set(x.get("members") or [])]


def online_members() -> list[str]:
    """Names of the members that can take an instance or a volume now."""
    return [m["server_name"] for m in cluster_members() if m.get("status") == "Online"]


@bp.context_processor
def _server_context():
    if "incus" not in g:
        return {}
    projects = [g.project]
    if not g.get("incus_failed"):
        try:
            projects = [p["name"] for p in g.incus.projects()] or projects
        except IncusError:
            pass
    projects = access.visible_projects(g.sid, projects) or [g.project]
    return {
        "server": g.server,
        "project": g.project,
        "projects": projects,
        "cluster_members": cluster_members(),
    }


DONE, RUNNING = "done", "running"


def _undone(label: str) -> str:
    """ "Container r10 created." -> "Container r10 created" for use inside a sentence."""
    return label.rstrip(".")


def start(call, done: str) -> str | None:
    """Run an Incus call that may start an operation and report the outcome.

    Returns DONE, RUNNING (still going after ACTION_WAIT seconds), or None if it failed.
    """
    try:
        response = call()
        finished = g.incus.wait(response, current_app.config["ACTION_WAIT"])
    except IncusError as error:
        flash(error.message, "error")
        return None
    if finished:
        flash(done, "success")
        return DONE
    op_id = response["metadata"]["id"]
    tracker.track(get_db(), current_app.config["DATA_DIR"], g.incus, g.sid, g.project, op_id, done)
    flash("Still running. You will see the outcome here when it finishes.", "info")
    return RUNNING


def run(call, done: str) -> bool:
    """Like start(), for callers that only care whether it went wrong."""
    return start(call, done) is not None
