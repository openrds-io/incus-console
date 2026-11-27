"""Roles enforced by Incus itself (server mode, remote servers, opt-in per server).

Without this, a user's restricted certificate keeps them inside their projects,
but inside a project it is only the console that tells a Viewer from a Project
admin. With it, the console writes an authorization scriptlet to the server:
a small program Incus runs for every request made with a restricted
certificate, which answers from the same roles the console uses. Incus then
refuses a Viewer's changes whatever the console (or a stolen certificate) asks.

The scriptlet is rewritten whenever access changes. Certificates the console
did not issue are treated as before: allowed inside the projects they list.
"""

from __future__ import annotations

import hashlib
import json

from flask import current_app

from . import access
from .db import get_db
from .incus import IncusError

NEEDS = ("authorization_scriptlet", "authorization_client_routing")
SCRIPTLET_KEY = "authorization.scriptlet"
ROUTING_KEY = "authorization.client.tls-restricted"

# The program itself. It is Starlark, which reads like Python; the tests run it
# as Python to check every decision. __ROLES__ and __LOG__ are filled in.
TEMPLATE = """# Written by Incus Console. Do not edit: it is replaced whenever access changes.
# Remove it with: incus config unset authorization.client.tls-restricted
#                 incus config unset authorization.scriptlet

LOG = __LOG__

# Certificate fingerprint -> {where: role}. Where is a project, "*" for all
# projects, or "project/instance" for a role on that one instance.
ROLES = __ROLES__
RANK = {"viewer": 1, "operator": 2, "project_admin": 3}

# Looking, for every role.
VIEW = [
    "can_view",
    "can_view_events",
    "can_view_operations",
    "can_view_resources",
    "can_view_metrics",
]
# Day-to-day work, for operators and project admins.
OPERATE = {
    "instance": [
        "can_update_state",
        "can_exec",
        "can_access_console",
        "can_access_files",
        "can_connect_sftp",
        "can_manage_snapshots",
    ],
    "storage_volume": ["can_manage_snapshots"],
}
# Kinds of object that belong to a project; their names start with it.
SCOPED = [
    "project",
    "instance",
    "profile",
    "image",
    "image_alias",
    "network",
    "network_acl",
    "network_address_set",
    "network_zone",
    "storage_volume",
    "storage_bucket",
]


def _username(details):
    if hasattr(details, "Username"):
        return details.Username
    return details.RequestDetails.Username


def _first(thing, names, default):
    for name in names:
        if hasattr(thing, name):
            return getattr(thing, name)
    return default


def _project_of(kind, path):
    if kind == "project":
        return path
    return path.split("/")[0]


def _inside(kind, entitlement):
    # Everything in the project, but the project's own settings stay with
    # console admins: creating things in it, yes; editing or deleting it, no.
    if kind == "project":
        return entitlement.startswith("can_create_")
    return True


def _stronger(one, other):
    if one == None:
        return other
    if other == None:
        return one
    if RANK[other] > RANK[one]:
        return other
    return one


def _has_instance_in(grants, project):
    for where in grants:
        if where.startswith(project + "/"):
            return True
    return False


def _as_listed(details, kind, path, entitlement):
    # A certificate the console did not issue: as Incus treats it without a
    # scriptlet, it may work inside the projects it lists.
    certificate = _first(details, ["Certificate", "certificate"], None)
    projects = _first(certificate, ["Projects", "projects"], None)
    if projects == None:
        return False
    if kind not in SCOPED:
        return entitlement in VIEW
    if _project_of(kind, path) not in projects:
        return False
    return entitlement in VIEW or _inside(kind, entitlement)


def _decide(details, object, entitlement):
    kind, _, path = object.partition(":")
    grants = ROLES.get(_username(details))
    if grants == None:
        return _as_listed(details, kind, path, entitlement)
    if kind not in SCOPED:
        # The server, its storage pools, cluster and so on: looking only.
        return entitlement in VIEW
    project = _project_of(kind, path)
    role = _stronger(grants.get(project), grants.get("*"))
    if kind == "instance":
        # "project/name", for snapshots and the like "project/name/...".
        parts = path.split("/")
        if len(parts) > 1:
            role = _stronger(role, grants.get(project + "/" + parts[1]))
    if role == None:
        # Someone given single instances may see that their project is there
        # (and hear of its operations and events), and nothing else in it.
        if kind == "project" and _has_instance_in(grants, project):
            return entitlement in VIEW
        return False
    if entitlement in VIEW:
        return True
    if role == "viewer":
        return False
    if entitlement in OPERATE.get(kind, []):
        return True
    if role == "operator":
        return False
    return _inside(kind, entitlement)


def authorize(details, object, entitlement):
    allowed = _decide(details, object, entitlement)
    if LOG:
        who = _username(details)
        log_info("incus-console authz: ", who, " ", object, " ", entitlement, " -> ", allowed)
    return allowed
"""


def supported(info: dict) -> bool:
    """Can this server route restricted certificates to a scriptlet?"""
    extensions = info.get("api_extensions") or []
    return all(name in extensions for name in NEEDS)


def roles(server_id: int) -> dict[str, dict[str, str]]:
    """{certificate fingerprint: {project or "*": role}} for the console's users."""
    found = {}
    rows = get_db().execute(
        "SELECT user_certs.user_id, user_certs.fingerprint FROM user_certs "
        "JOIN users ON users.id = user_certs.user_id "
        "WHERE user_certs.server_id = ? AND users.disabled = 0 AND users.is_admin = 0 "
        "ORDER BY user_certs.fingerprint",
        (server_id,),
    )
    for row in rows.fetchall():
        grants = {
            entry["project"]: entry["role"]
            for entry in access.effective_grants(row["user_id"], server_id)
        }
        for given in access.instance_grants_for(row["user_id"], server_id):
            grants[f"{given['project']}/{given['instance']}"] = given["role"]
        if grants:
            found[row["fingerprint"]] = dict(sorted(grants.items()))
    return found


def scriptlet(server_id: int) -> str:
    """The scriptlet for this server as its users and roles stand now."""
    log = "True" if current_app.config["AUTHZ_SCRIPTLET_LOG"] else "False"
    table = json.dumps(roles(server_id), indent=4, sort_keys=True)
    return TEMPLATE.replace("__LOG__", log).replace("__ROLES__", table)


def _console(server):
    from .servers import client_for

    return client_for(server)


def _remember(server_id: int, enforced: bool, text: str | None) -> None:
    digest = hashlib.sha256(text.encode()).hexdigest() if text else None
    db = get_db()
    db.execute(
        "UPDATE servers SET enforce_roles = ?, scriptlet_hash = ? WHERE id = ?",
        (int(enforced), digest, server_id),
    )
    db.commit()


def enable(server) -> None:
    """Install the scriptlet and send restricted certificates to it. Raises IncusError."""
    client = _console(server)
    if not supported(client.server_info()):
        raise IncusError(
            400,
            "This Incus cannot do it: it needs the authorization scriptlet with client "
            "routing (a recent Incus 7).",
        )
    text = scriptlet(server["id"])
    # The scriptlet first, so nobody is routed to one that is not there yet.
    client.update_server_config({SCRIPTLET_KEY: text})
    client.update_server_config({ROUTING_KEY: "scriptlet"})
    _remember(server["id"], True, text)


def disable(server) -> None:
    """Back to Incus's own handling of restricted certificates. Raises IncusError."""
    client = _console(server)
    client.update_server_config({ROUTING_KEY: ""})
    client.update_server_config({SCRIPTLET_KEY: ""})
    _remember(server["id"], False, None)


def refresh(server) -> None:
    """Rewrite the scriptlet if access changed since it was last sent. Raises IncusError."""
    row = get_db().execute("SELECT * FROM servers WHERE id = ?", (server["id"],)).fetchone()
    if row is None or not row["enforce_roles"]:
        return
    text = scriptlet(server["id"])
    if hashlib.sha256(text.encode()).hexdigest() == row["scriptlet_hash"]:
        return
    _console(server).update_server_config({SCRIPTLET_KEY: text})
    _remember(server["id"], True, text)
