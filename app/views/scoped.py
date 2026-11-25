"""Pages that belong to one registered server live under /servers/<sid>/."""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, g, request, session

from .. import servers
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
    key = f"project:{g.sid}"
    if request.args.get("project"):
        session[key] = request.args["project"]
    g.project = session.get(key, "default")
    g.incus = servers.client_for(server, g.project)


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
    return {"server": g.server, "project": g.project, "projects": projects}


def run(call, done: str) -> bool:
    """Run an Incus call that may start an operation and report the outcome."""
    try:
        finished = g.incus.wait(call(), current_app.config["ACTION_WAIT"])
    except IncusError as error:
        flash(error.message, "error")
        return False
    if finished:
        flash(done, "success")
    else:
        flash("Still running. See Operations for progress.", "info")
    return True
