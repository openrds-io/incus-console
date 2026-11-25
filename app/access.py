"""Who may do what: console admins, and per-server, per-project roles.

Roles, weakest first:
  viewer         sees everything in the project; changes nothing, no terminal or files
  operator       viewer, plus start/stop/restart, snapshots, terminal and files
  project_admin  everything inside the project
Console admins can do everything, including servers, users and project settings.

Every page under /servers/<sid>/ needs one capability. Pages that accept POST need
"manage" unless listed below, so a new page is closed to viewers by default.
Desktop mode has one user, who is an admin.
"""

from __future__ import annotations

import sqlite3

from flask import current_app, g

from .db import get_db

ROLES = {"viewer": 1, "operator": 2, "project_admin": 3}
ROLE_LABELS = {"viewer": "Viewer", "operator": "Operator", "project_admin": "Project admin"}
CAPABILITIES = {"view": 1, "operate": 2, "manage": 3}
ALL_PROJECTS = "*"

REQUIRED = {
    # Changing projects themselves, and server-wide storage, is for console admins.
    # Incus refuses these to project-restricted certificates too.
    "srv.project_create": "admin",
    "srv.project_edit": "admin",
    "srv.project_yaml": "admin",
    "srv.project_rename": "admin",
    "srv.project_delete": "admin",
    "srv.project_use": "view",
    # Day-to-day work on instances.
    "srv.instance_action": "operate",
    "srv.instance_terminal": "operate",
    "srv.instance_terminal_ws": "operate",
    "srv.snapshot_create": "operate",
    "srv.volume_snapshot_create": "operate",
    "srv.operation_cancel": "operate",
    # Files inside an instance can hold secrets, so even reading them is operator work.
    "srv.files": "operate",
    "srv.file_view": "operate",
    "srv.file_download": "operate",
    "srv.file_edit": "operate",
    "srv.file_upload": "operate",
    "srv.file_mkdir": "operate",
    "srv.file_delete": "operate",
}


def is_admin(user=None) -> bool:
    user = user if user is not None else g.get("user")
    return bool(user and user["is_admin"])


def required(endpoint: str) -> str:
    if endpoint in REQUIRED:
        return REQUIRED[endpoint]
    rules = current_app.url_map._rules_by_endpoint.get(endpoint, [])
    if any("POST" in rule.methods for rule in rules):
        return "manage"
    return "view"


def grants_for(user_id: int, server_id: int | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT grants.*, servers.name AS server_name, servers.kind AS server_kind FROM grants "
        "JOIN servers ON servers.id = grants.server_id WHERE user_id = ?"
    )
    args: list = [user_id]
    if server_id is not None:
        sql += " AND server_id = ?"
        args.append(server_id)
    return get_db().execute(sql + " ORDER BY servers.name, project", args).fetchall()


def _user_grants(server_id: int) -> dict[str, int]:
    """{project: rank} for the signed-in user on one server, cached per request."""
    cache = g.setdefault("_grants", {})
    if server_id not in cache:
        cache[server_id] = {
            row["project"]: ROLES[row["role"]] for row in grants_for(g.user["id"], server_id)
        }
    return cache[server_id]


def rank(server_id: int, project: str) -> int:
    if is_admin():
        return 99
    if not g.get("user"):
        return 0
    found = _user_grants(server_id)
    return max(found.get(project, 0), found.get(ALL_PROJECTS, 0))


def can(capability: str, project: str | None = None, server_id: int | None = None) -> bool:
    if is_admin():
        return True
    if capability == "admin":
        return False
    server_id = server_id if server_id is not None else g.get("sid")
    project = project if project is not None else g.get("project")
    if server_id is None or project is None:
        return False
    return rank(server_id, project) >= CAPABILITIES[capability]


def allowed(endpoint: str, project: str | None = None) -> bool:
    """For templates: would the signed-in user be let through to this page?"""
    return can(required(endpoint), project)


def visible_projects(server_id: int, names: list[str]) -> list[str]:
    if is_admin():
        return names
    found = _user_grants(server_id)
    if ALL_PROJECTS in found:
        return names
    return [name for name in names if name in found]


def granted_projects(server_id: int) -> list[str]:
    """Projects named in the user's grants (not "all projects")."""
    return sorted(p for p in _user_grants(server_id) if p != ALL_PROJECTS)


def visible_servers(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    if is_admin():
        return rows
    if not g.get("user"):
        return []
    ids = {
        row["server_id"]
        for row in get_db().execute(
            "SELECT DISTINCT server_id FROM grants WHERE user_id = ?", (g.user["id"],)
        )
    }
    return [row for row in rows if row["id"] in ids]


def init_app(app) -> None:
    app.jinja_env.globals.update(
        can=can, allowed=allowed, is_admin=is_admin, role_labels=ROLE_LABELS
    )
