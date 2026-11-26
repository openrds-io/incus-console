"""Who may do what: console admins, and per-server, per-project roles.

A user's access is their own grants plus the grants of every group they belong to.
Where several give a role in the same server and project, the strongest wins.
Groups carry project roles only; being a console admin is always per user.

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
    # Taking a cluster member out of service affects every project on it.
    "srv.cluster_evacuate": "admin",
    "srv.cluster_restore": "admin",
    # Day-to-day work on instances.
    "srv.instance_action": "operate",
    "srv.instance_terminal": "operate",
    "srv.instance_terminal_ws": "operate",
    "srv.instance_console": "operate",
    "srv.instance_console_ws": "operate",
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
    """The user's own grants, not counting their groups'."""
    sql = (
        "SELECT grants.*, servers.name AS server_name, servers.kind AS server_kind FROM grants "
        "JOIN servers ON servers.id = grants.server_id WHERE user_id = ?"
    )
    args: list = [user_id]
    if server_id is not None:
        sql += " AND server_id = ?"
        args.append(server_id)
    return get_db().execute(sql + " ORDER BY servers.name, project", args).fetchall()


def group_grants_for(user_id: int, server_id: int | None = None) -> list[sqlite3.Row]:
    """Grants the user has through their groups, one row per group and grant."""
    sql = (
        "SELECT group_grants.*, user_groups.name AS group_name, servers.name AS server_name, "
        "servers.kind AS server_kind FROM group_grants "
        "JOIN group_members ON group_members.group_id = group_grants.group_id "
        "JOIN user_groups ON user_groups.id = group_grants.group_id "
        "JOIN servers ON servers.id = group_grants.server_id WHERE group_members.user_id = ?"
    )
    args: list = [user_id]
    if server_id is not None:
        sql += " AND group_grants.server_id = ?"
        args.append(server_id)
    order = " ORDER BY servers.name, project, user_groups.name"
    return get_db().execute(sql + order, args).fetchall()


def effective_grants(user_id: int, server_id: int | None = None) -> list[dict]:
    """The user's access from every source: one entry per server and project.

    Each entry has server_id, server_name, server_kind, project, role (the strongest)
    and via: where it comes from, None for the user's own grant, else group names.
    """
    found: dict[tuple[int, str], dict] = {}

    def add(row, source):
        key = (row["server_id"], row["project"])
        entry = found.setdefault(
            key,
            {
                "server_id": row["server_id"],
                "server_name": row["server_name"],
                "server_kind": row["server_kind"],
                "project": row["project"],
                "role": row["role"],
                "via": [],
            },
        )
        if ROLES[row["role"]] > ROLES[entry["role"]]:
            entry["role"] = row["role"]
        entry["via"].append(source)

    for row in grants_for(user_id, server_id):
        add(row, None)
    for row in group_grants_for(user_id, server_id):
        add(row, row["group_name"])
    return sorted(found.values(), key=lambda e: (e["server_name"], e["project"]))


def users_with_access(server_id: int) -> list[int]:
    """Everyone given any role on the server, directly or through a group."""
    rows = get_db().execute(
        "SELECT user_id FROM grants WHERE server_id = ? "
        "UNION SELECT group_members.user_id FROM group_grants "
        "JOIN group_members ON group_members.group_id = group_grants.group_id "
        "WHERE group_grants.server_id = ?",
        (server_id, server_id),
    )
    return [row[0] for row in rows]


def _user_grants(server_id: int) -> dict[str, int]:
    """{project: rank} for the signed-in user on one server, cached per request."""
    cache = g.setdefault("_grants", {})
    if server_id not in cache:
        cache[server_id] = {
            entry["project"]: ROLES[entry["role"]]
            for entry in effective_grants(g.user["id"], server_id)
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
    ids = {entry["server_id"] for entry in effective_grants(g.user["id"])}
    return [row for row in rows if row["id"] in ids]


def init_app(app) -> None:
    app.jinja_env.globals.update(
        can=can, allowed=allowed, is_admin=is_admin, role_labels=ROLE_LABELS
    )
