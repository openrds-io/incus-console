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
# Roles that can be given on a single instance. Reconfiguring, copying or
# deleting one touches the project around it, so that stays a project role.
INSTANCE_ROLES = ("viewer", "operator")

REQUIRED = {
    # Changing projects themselves, and server-wide storage, is for console admins.
    # Incus refuses these to project-restricted certificates too.
    "srv.project_create": "admin",
    "srv.project_edit": "admin",
    "srv.project_yaml": "admin",
    "srv.project_rename": "admin",
    "srv.project_delete": "admin",
    "srv.project_use": "view",
    # Who can reach a project is its admins' business, not every member's.
    "srv.project_access": "manage",
    "srv.instance_access": "manage",
    "srv.pool_create": "admin",
    "srv.pool_delete": "admin",
    # Taking a cluster member out of service affects every project on it.
    "srv.cluster_evacuate": "admin",
    "srv.cluster_restore": "admin",
    "srv.cluster_member_edit": "admin",
    "srv.cluster_member_remove": "admin",
    "srv.cluster_group_create": "admin",
    "srv.cluster_group_edit": "admin",
    "srv.cluster_group_delete": "admin",
    # Starting a cluster, and bringing servers into it.
    "srv.cluster_enable": "admin",
    "srv.cluster_join": "admin",
    "srv.cluster_token_create": "admin",
    "srv.cluster_token_revoke": "admin",
    "srv.cluster_settings": "admin",
    "srv.cluster_upgrade": "admin",
    "srv.cluster_certificate": "admin",
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


def who_can_reach(server_id: int, project: str, instance: str | None = None) -> list[dict]:
    """Everyone who can open this project through the console, strongest role first.

    Each entry: user_id, username, role ("admin" for console admins), via (a
    list: None for the user's own grant, else the group's name), and scope
    ("this project" or "all projects"). Disabled users are left out.
    """
    db = get_db()
    people: dict[int, dict] = {}
    for user in db.execute("SELECT id, username FROM users WHERE is_admin = 1 AND disabled = 0"):
        people[user["id"]] = {
            "user_id": user["id"],
            "username": user["username"],
            "role": "admin",
            "via": [],
            "scope": "all projects",
        }
    rows = db.execute(
        "SELECT users.id AS user_id, users.username, grants.role, grants.project, "
        "NULL AS group_name FROM grants JOIN users ON users.id = grants.user_id "
        "WHERE grants.server_id = ? AND grants.project IN (?, ?) AND users.disabled = 0 "
        "UNION ALL "
        "SELECT users.id, users.username, group_grants.role, group_grants.project, "
        "user_groups.name FROM group_grants "
        "JOIN group_members ON group_members.group_id = group_grants.group_id "
        "JOIN user_groups ON user_groups.id = group_grants.group_id "
        "JOIN users ON users.id = group_members.user_id "
        "WHERE group_grants.server_id = ? AND group_grants.project IN (?, ?) "
        "AND users.disabled = 0",
        (server_id, project, ALL_PROJECTS, server_id, project, ALL_PROJECTS),
    ).fetchall()
    for row in rows:
        entry = people.setdefault(
            row["user_id"],
            {
                "user_id": row["user_id"],
                "username": row["username"],
                "role": row["role"],
                "via": [],
                "scope": "this project",
            },
        )
        if entry["role"] == "admin":
            continue
        if ROLES[row["role"]] > ROLES[entry["role"]]:
            entry["role"] = row["role"]
        if row["project"] == ALL_PROJECTS:
            entry["scope"] = "all projects"
        if row["group_name"] not in entry["via"]:
            entry["via"].append(row["group_name"])
    rank_of = {"admin": 99, **ROLES}
    if instance:
        where = (server_id, project, instance)
        given = db.execute(
            "SELECT users.id AS user_id, users.username, instance_grants.role, "
            "NULL AS group_name FROM instance_grants "
            "JOIN users ON users.id = instance_grants.user_id "
            "WHERE instance_grants.server_id = ? AND instance_grants.project = ? "
            "AND instance_grants.instance = ? AND users.disabled = 0 "
            "UNION ALL "
            "SELECT users.id, users.username, group_instance_grants.role, user_groups.name "
            "FROM group_instance_grants "
            "JOIN group_members ON group_members.group_id = group_instance_grants.group_id "
            "JOIN user_groups ON user_groups.id = group_instance_grants.group_id "
            "JOIN users ON users.id = group_members.user_id "
            "WHERE group_instance_grants.server_id = ? AND group_instance_grants.project = ? "
            "AND group_instance_grants.instance = ? AND users.disabled = 0",
            (*where, *where),
        ).fetchall()
        for row in given:
            entry = people.get(row["user_id"])
            if entry is None:
                people[row["user_id"]] = {
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "role": row["role"],
                    "via": [row["group_name"]],
                    "scope": "this instance",
                }
            elif entry["scope"] == "this instance":
                if rank_of[row["role"]] > rank_of[entry["role"]]:
                    entry["role"] = row["role"]
                if row["group_name"] not in entry["via"]:
                    entry["via"].append(row["group_name"])
            elif rank_of[row["role"]] > rank_of[entry["role"]]:
                entry.update(role=row["role"], scope="this instance", via=[row["group_name"]])
    return sorted(people.values(), key=lambda e: (-rank_of[e["role"]], e["username"].lower()))


def users_with_access(server_id: int) -> list[int]:
    """Everyone given any role on the server, directly or through a group."""
    rows = get_db().execute(
        "SELECT user_id FROM grants WHERE server_id = ? "
        "UNION SELECT user_id FROM instance_grants WHERE server_id = ? "
        "UNION SELECT group_members.user_id FROM group_grants "
        "JOIN group_members ON group_members.group_id = group_grants.group_id "
        "WHERE group_grants.server_id = ? "
        "UNION SELECT group_members.user_id FROM group_instance_grants "
        "JOIN group_members ON group_members.group_id = group_instance_grants.group_id "
        "WHERE group_instance_grants.server_id = ?",
        (server_id, server_id, server_id, server_id),
    )
    return [row[0] for row in rows]


def instance_grants_for(user_id: int, server_id: int | None = None) -> list[sqlite3.Row]:
    """The user's roles on single instances."""
    sql = (
        "SELECT instance_grants.*, servers.name AS server_name, servers.kind AS server_kind "
        "FROM instance_grants JOIN servers ON servers.id = instance_grants.server_id "
        "WHERE user_id = ?"
    )
    args: list = [user_id]
    if server_id is not None:
        sql += " AND server_id = ?"
        args.append(server_id)
    return get_db().execute(sql + " ORDER BY servers.name, project, instance", args).fetchall()


def group_instance_grants_for(user_id: int, server_id: int | None = None) -> list[sqlite3.Row]:
    """Roles on single instances the user has through their groups."""
    sql = (
        "SELECT group_instance_grants.*, user_groups.name AS group_name, "
        "servers.name AS server_name FROM group_instance_grants "
        "JOIN group_members ON group_members.group_id = group_instance_grants.group_id "
        "JOIN user_groups ON user_groups.id = group_instance_grants.group_id "
        "JOIN servers ON servers.id = group_instance_grants.server_id "
        "WHERE group_members.user_id = ?"
    )
    args: list = [user_id]
    if server_id is not None:
        sql += " AND group_instance_grants.server_id = ?"
        args.append(server_id)
    order = " ORDER BY servers.name, project, instance, user_groups.name"
    return get_db().execute(sql + order, args).fetchall()


def effective_instance_grants(user_id: int, server_id: int | None = None) -> list[dict]:
    """The user's roles on single instances from every source, the strongest of each.

    Each entry has server_id, project, instance, role, and via: None for the
    user's own grant, else group names.
    """
    found: dict[tuple[int, str, str], dict] = {}

    def add(row, source):
        key = (row["server_id"], row["project"], row["instance"])
        entry = found.setdefault(
            key,
            {
                "server_id": row["server_id"],
                "project": row["project"],
                "instance": row["instance"],
                "role": row["role"],
                "via": [],
            },
        )
        if ROLES[row["role"]] > ROLES[entry["role"]]:
            entry["role"] = row["role"]
        entry["via"].append(source)

    for row in instance_grants_for(user_id, server_id):
        add(row, None)
    for row in group_instance_grants_for(user_id, server_id):
        add(row, row["group_name"])
    return sorted(found.values(), key=lambda e: (e["server_id"], e["project"], e["instance"]))


def _own_instances(server_id: int) -> dict[tuple[str, str], int]:
    """{(project, instance): rank} for the signed-in user on one server, cached per request."""
    cache = g.setdefault("_instance_grants", {})
    if server_id not in cache:
        cache[server_id] = {
            (entry["project"], entry["instance"]): ROLES[entry["role"]]
            for entry in effective_instance_grants(g.user["id"], server_id)
        }
    return cache[server_id]


def enters(server_id: int, project: str) -> bool:
    """May the user open this project at all: a role in it, or on an instance in it?"""
    if rank(server_id, project) >= CAPABILITIES["view"]:
        return True
    if is_admin() or not g.get("user"):
        return False
    return any(found == project for found, _instance in _own_instances(server_id))


def visible_instances(server_id: int, project: str, rows: list) -> list:
    """The instances of a project the user may see: all, or only those they were given."""
    if rank(server_id, project) >= CAPABILITIES["view"]:
        return rows
    own = _own_instances(server_id) if g.get("user") else {}
    return [row for row in rows if (project, row["name"]) in own]


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


def can(
    capability: str,
    project: str | None = None,
    server_id: int | None = None,
    instance: str | None = None,
) -> bool:
    """``instance``: also count a role the user has on that one instance."""
    if is_admin():
        return True
    if capability == "admin":
        return False
    server_id = server_id if server_id is not None else g.get("sid")
    project = project if project is not None else g.get("project")
    if server_id is None or project is None:
        return False
    best = rank(server_id, project)
    if instance and g.get("user"):
        best = max(best, _own_instances(server_id).get((project, instance), 0))
    return best >= CAPABILITIES[capability]


def allowed(endpoint: str, project: str | None = None, instance: str | None = None) -> bool:
    """For templates: would the signed-in user be let through to this page?

    On an instance's own pages the instance is the one being shown.
    """
    if instance is None and project is None:
        instance = g.get("instance_name")
    need = required(endpoint)
    if instance and need in ("view", "operate") and is_instance_page(endpoint):
        return can(need, project, instance=instance)
    return can(need, project)


def is_instance_page(endpoint: str) -> bool:
    """Is this one of the pages under /instances/<name>?"""
    rules = current_app.url_map._rules_by_endpoint.get(endpoint, [])
    return any("/instances/<name>" in rule.rule for rule in rules)


def visible_projects(server_id: int, names: list[str]) -> list[str]:
    if is_admin():
        return names
    found = _user_grants(server_id)
    if ALL_PROJECTS in found:
        return names
    partly = {project for project, _instance in _own_instances(server_id)}
    return [name for name in names if name in found or name in partly]


def granted_projects(server_id: int) -> list[str]:
    """Projects named in the user's grants (not "all projects"), or holding an
    instance they were given."""
    named = {p for p in _user_grants(server_id) if p != ALL_PROJECTS}
    return sorted(named | {project for project, _instance in _own_instances(server_id)})


def visible_servers(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    if is_admin():
        return rows
    if not g.get("user"):
        return []
    ids = {entry["server_id"] for entry in effective_grants(g.user["id"])}
    ids |= {entry["server_id"] for entry in effective_instance_grants(g.user["id"])}
    return [row for row in rows if row["id"] in ids]


def init_app(app) -> None:
    app.jinja_env.globals.update(
        can=can, allowed=allowed, is_admin=is_admin, role_labels=ROLE_LABELS
    )
