"""Clusters: the members of the cluster a server belongs to, and their groups."""

from __future__ import annotations

import datetime
import hashlib
import re
import ssl
import time
from urllib.parse import urlsplit

from flask import abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import access, servers
from ..incus import IncusConnectionError, IncusError
from ..incus.client import is_join_token, join_token, normalize_fingerprint, peer_fingerprint
from .config_forms import settings_fields
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
MEMBER_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,61}[A-Za-z0-9])?")
MEMBER_RULE = "A member name uses letters, digits, dashes and dots (at most 63), like a host name."
ADDRESS_RULE = (
    "Give the address other members reach it at, such as 192.168.64.5:8443 "
    "(not a wildcard like :8443)."
)
PORT = "8443"
# Listening on every address: fine for one server, no use as a cluster address.
WILDCARDS = ("0.0.0.0", "::", "*")  # noqa: S104
# How long to wait for a server that is not answering, when listing the ones that could join.
PROBE_TIMEOUT = 5


def _counts() -> dict[str, int]:
    """Instances of the current project on each member."""
    counts: dict[str, int] = {}
    for instance in g.incus.instances():
        location = instance.get("location") or ""
        counts[location] = counts.get(location, 0) + 1
    return counts


# Cluster-wide settings: (key, label, kind, help), as config_forms.SETTINGS.
CLUSTER_SETTINGS = [
    (
        "cluster.offline_threshold",
        "Offline after (seconds)",
        "text",
        "How long a member may miss its heartbeats before it counts as offline.",
    ),
    (
        "cluster.healing_threshold",
        "Evacuate an offline member after (seconds)",
        "text",
        "Incus then restarts its instances on other members. They need shared storage, "
        "such as Ceph, to come back with their data. 0 or empty: never.",
    ),
    (
        "cluster.max_voters",
        "Database voters",
        "text",
        "How many members hold a vote in the cluster's database: an odd number, 3 or more.",
    ),
    (
        "cluster.max_standby",
        "Database stand-bys",
        "text",
        "Members that keep a copy of the database, ready to become voters.",
    ),
    (
        "cluster.images_minimal_replica",
        "Copies of each image",
        "text",
        "On how many members an image is kept. -1: on every member.",
    ),
    (
        "cluster.join_token_expiry",
        "Join tokens last",
        "text",
        "Such as 3H, 1d or 30M (hours, days, minutes).",
    ),
]
CLUSTER_DEFAULTS = {
    "cluster.offline_threshold": "20",
    "cluster.healing_threshold": "0",
    "cluster.max_voters": "3",
    "cluster.max_standby": "2",
    "cluster.images_minimal_replica": "3",
    "cluster.join_token_expiry": "3H",
}
WHOLE_NUMBER = re.compile(r"\d+")
DURATION = re.compile(r"\d+[SMHdwmy]")
# Where Incus places new instances: on any member, only when asked by name, or
# only as a member of one of its groups.
SCHEDULER_CHOICES = ("all", "manual", "group")


def _check_cluster_setting(key: str, value: str) -> str | None:
    """Why Incus would refuse the value, in plain words; None if it is fine."""
    if key == "cluster.join_token_expiry":
        return None if DURATION.fullmatch(value) else "Join tokens last: use a form like 3H."
    if key == "cluster.images_minimal_replica":
        ok = value == "-1" or (WHOLE_NUMBER.fullmatch(value) and int(value) >= 1)
        return None if ok else "Copies of each image: 1 or more, or -1."
    if not WHOLE_NUMBER.fullmatch(value):
        label = next(label for k, label, _kind, _help in CLUSTER_SETTINGS if k == key)
        return f"{label}: a whole number."
    if key == "cluster.max_voters" and (int(value) < 3 or int(value) % 2 == 0):
        return "Database voters: an odd number, 3 or more."
    return None


@bp.route("/cluster/settings", methods=["GET", "POST"])
def cluster_settings():
    if not cluster_members():
        abort(404)
    config = g.incus.server_info().get("config") or {}
    if request.method == "POST":
        changes = {
            key: request.form.get(f"cfg:{key}", "").strip()
            for key, _label, _kind, _help in CLUSTER_SETTINGS
        }
        problems = [p for k, v in changes.items() if v and (p := _check_cluster_setting(k, v))]
        # Only what changed: an empty field unsets the key, back to Incus's default.
        changes = {k: v for k, v in changes.items() if v != config.get(k, "")}
        for problem in problems:
            flash(problem, "error")
        if not problems and (
            not changes
            or run(lambda: g.incus.update_server_config(changes), "Cluster settings saved.")
        ):
            return redirect(url_for("srv.cluster"))
        config = {**config, **changes}
    fields = settings_fields(config, settings=CLUSTER_SETTINGS, defaults=CLUSTER_DEFAULTS)
    for field in fields:
        field["empty"] = f"Default: {field['default']}"
    return render_template("cluster/settings.html", fields=fields)


# What the roles Incus gives out mean for a member's share of the database.
DATABASE_ROLES = (
    ("database-leader", "Leader"),
    ("database", "Voter"),
    ("database-standby", "Stand-by"),
)


def database_role(member: dict) -> str:
    """Leader, Voter, Stand-by or Spare: how the member takes part in the database."""
    roles = member.get("roles") or []
    return next((label for role, label in DATABASE_ROLES if role in roles), "Spare")


def _versions(members: list[dict]) -> dict[str, str]:
    """The Incus version each member that answers runs."""
    found = {}
    for member in members:
        if member.get("status") == "Offline":
            continue
        try:
            info = g.incus.server_info(target=member["server_name"])
        except IncusError:
            continue
        version = (info.get("environment") or {}).get("server_version")
        if version:
            found[member["server_name"]] = version
    return found


def _mixed(versions: dict[str, str]) -> str:
    """'7.7 on incus1, incus2; 7.8 on incus3' when the versions differ, else ''."""
    by_version: dict[str, list[str]] = {}
    for name, version in sorted(versions.items()):
        by_version.setdefault(version, []).append(name)
    if len(by_version) < 2:
        return ""
    return "; ".join(f"{v} on {', '.join(names)}" for v, names in sorted(by_version.items()))


@bp.get("/cluster")
def cluster():
    members = cluster_members()
    admin = access.is_admin()
    problem = g.get("cluster_error")
    versions = _versions(members) if members else {}
    return render_template(
        "cluster/members.html",
        members=members,
        versions=versions,
        mixed=_mixed(versions),
        database_role=database_role,
        groups=cluster_groups(),
        counts=_counts() if members else {},
        tokens=_tokens() if members and admin else [],
        enable=_enable_defaults() if admin and not members and not problem else None,
        problem=problem,
    )


# -- addresses ----------------------------------------------------------------


def listen_address(value: str) -> str | None:
    """'10.0.0.5:8443' from '10.0.0.5', 'https://10.0.0.5:8443/' and the like.

    None for a wildcard (":8443", "0.0.0.0") or something that is no address:
    the other members must be able to reach this one at it.
    """
    value = (value or "").strip().removeprefix("https://").rstrip("/")
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest.removeprefix(":")
    elif value.count(":") == 1:
        host, port = value.split(":")
    else:
        host, port = value, ""
    port = port or PORT
    wildcard = host in WILDCARDS
    if not host or wildcard or any(c.isspace() or c in "/?#" for c in host):
        return None
    if not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _registered_address(server) -> str | None:
    """host:port the console reaches an https server at; None for a local socket."""
    if server["kind"] != "https":
        return None
    parts = urlsplit(server["address"])
    return listen_address(f"{parts.hostname}:{parts.port or PORT}") if parts.hostname else None


def _suggested_address(info: dict, server) -> str:
    current = (info.get("config") or {}).get("core.https_address", "")
    if listen_address(current):
        return listen_address(current)
    registered = _registered_address(server)
    if registered:
        return registered
    for address in (info.get("environment") or {}).get("addresses") or []:
        found = listen_address(address)
        if found and not found.startswith(("127.", "[::1]")):
            return found
    return ""


# -- starting a cluster -------------------------------------------------------


def _enable_defaults() -> dict | None:
    try:
        info = g.incus.server_info()
    except IncusError:
        return None
    return {
        "name": (info.get("environment") or {}).get("server_name") or "",
        "address": _suggested_address(info, g.server),
        "current": (info.get("config") or {}).get("core.https_address", ""),
    }


@bp.post("/cluster/enable")
def cluster_enable():
    if cluster_members():
        flash(f"{g.server['name']} is already part of a cluster.", "error")
        return redirect(url_for("srv.cluster"))
    name = request.form.get("server_name", "").strip()
    address = listen_address(request.form.get("address", ""))
    if not MEMBER_NAME.fullmatch(name):
        flash(MEMBER_RULE, "error")
        return redirect(url_for("srv.cluster"))
    if address is None:
        flash(ADDRESS_RULE, "error")
        return redirect(url_for("srv.cluster"))
    try:
        current = (g.incus.server_info().get("config") or {}).get("core.https_address", "")
        if listen_address(current) != address:
            registered = _registered_address(g.server)
            if registered and registered != address:
                flash(
                    f"The console reaches {g.server['name']} at {registered}. Listening at "
                    f"{address} instead would cut the console off; use {registered}, or "
                    "register the server by its new address first.",
                    "error",
                )
                return redirect(url_for("srv.cluster"))
            g.incus.update_server_config({"core.https_address": address})
    except IncusError as error:
        flash(error.message, "error")
        return redirect(url_for("srv.cluster"))
    payload = {"server_name": name, "enabled": True}
    try:
        response = g.incus.update_cluster(payload)
        finished = _wait_for_join(g.incus, response)
    except IncusError as error:
        flash(error.message, "error")
        return redirect(url_for("srv.cluster"))
    if g.server["kind"] == "https":
        _follow_new_certificate(g.server)
    if finished:
        flash(f"Cluster started: {g.server['name']} is its first member, {name}.", "success")
    else:
        flash(f"The cluster is starting on {g.server['name']}.", "info")
    return redirect(url_for("srv.cluster"))


def _follow_new_certificate(server, tries: int = 15) -> None:
    """Starting a cluster gives the server a new certificate, the cluster's.

    Pin it, for every registration at that address, once the server behind
    it is shown to trust this console and to be clustered: the change was
    asked for a moment ago, by this console, over the old pinned certificate.
    """
    for attempt in range(tries):
        try:
            found = peer_fingerprint(server["address"], PROBE_TIMEOUT)
        except IncusError:
            found = None
        if found and found != server["fingerprint"]:
            break
        if attempt + 1 < tries:
            time.sleep(1)
    else:
        return
    client = servers.client_for({**dict(server), "fingerprint": found})
    try:
        info = client.server_info()
    except IncusError:
        return
    clustered = (info.get("environment") or {}).get("server_clustered")
    if info.get("auth") != "trusted" or not clustered:
        return
    for row in servers.all_servers():
        if row["kind"] == "https" and row["address"].rstrip("/") == server["address"].rstrip("/"):
            servers.set_fingerprint(row["id"], found)


# -- join tokens --------------------------------------------------------------

_ISO = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?")


def in_utc(value: str) -> str:
    """'2026-11-28 13:07 UTC' from Incus's '2026-11-28T08:07:54.608212927-05:00'."""
    found = _ISO.fullmatch(value or "")
    if not found:
        return value or ""
    offset = (found[2] or "Z").replace("Z", "+00:00")
    when = datetime.datetime.fromisoformat(found[1] + offset)
    return when.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


def preseed(token: str, member_config: list[dict]) -> str:
    """What ``incus admin init --preseed`` needs to join with this token."""
    lines = [
        "cluster:",
        "  enabled: true",
        "  server_address: ITS-ADDRESS:8443",
        f"  cluster_token: {token}",
    ]
    if member_config:
        lines.append("  member_config:")
        for item in member_config:
            lines += [
                f"  - entity: {item.get('entity', '')}",
                f"    name: {item.get('name', '')}",
                f"    key: {item.get('key', '')}",
                f'    value: "{item.get("value") or ""}"',
            ]
    return "\n".join(lines) + "\n"


def _tokens() -> list[dict]:
    """Join tokens handed out and not used yet."""
    try:
        operations = g.incus.operations()
    except IncusError:
        return []
    rows = []
    for op in operations:
        if is_join_token(op) and op.get("status") == "Running":
            meta = op["metadata"]
            rows.append(
                {
                    "id": op["id"],
                    "name": meta.get("serverName", ""),
                    "expires": in_utc(meta.get("expiresAt") or ""),
                    "token": join_token(op),
                }
            )
    return sorted(rows, key=lambda row: row["name"])


def _new_member_name(name: str) -> bool:
    """Flash why a name cannot be a new member's; True when it can."""
    if not MEMBER_NAME.fullmatch(name):
        flash(MEMBER_RULE, "error")
        return False
    if any(m["server_name"] == name for m in cluster_members()):
        flash(f"{name} is already a member.", "error")
        return False
    return True


def _member_config() -> list[dict]:
    """What a joining server must answer: per-member settings of pools and networks."""
    try:
        return list(g.incus.cluster().get("member_config") or [])
    except IncusError:
        return []


@bp.post("/cluster/tokens")
def cluster_token_create():
    if not cluster_members():
        abort(404)
    name = request.form.get("name", "").strip()
    if not _new_member_name(name):
        return redirect(url_for("srv.cluster"))
    try:
        operation = g.incus.add_cluster_member(name).get("metadata") or {}
    except IncusError as error:
        flash(error.message, "error")
        return redirect(url_for("srv.cluster"))
    meta = operation.get("metadata") or {}
    token = join_token(operation)
    member_config = _member_config()
    return render_template(
        "cluster/token.html",
        name=name,
        token=token,
        expires=in_utc(meta.get("expiresAt") or ""),
        member_config=member_config,
        preseed=preseed(token, member_config),
    )


@bp.post("/cluster/tokens/<op_id>/revoke")
def cluster_token_revoke(op_id):
    found = next((t for t in _tokens() if t["id"] == op_id), None)
    if found is None:
        abort(404)
    try:
        g.incus.cancel_operation(op_id)
    except IncusError as error:
        flash(error.message, "error")
    else:
        flash(f"The join token for {found['name']} is revoked.", "success")
    return redirect(url_for("srv.cluster"))


# -- joining a registered server --------------------------------------------


def _cluster_version() -> str:
    try:
        return (g.incus.server_info().get("environment") or {}).get("server_version") or ""
    except IncusError:
        return ""


def _probe(row, cluster_version: str = "") -> dict:
    """A registered server as a possible new member: its details, or why it cannot join."""
    entry = {"id": row["id"], "name": row["name"], "info": {}, "problem": None}
    client = servers.client_for(row)
    client.timeout = min(client.timeout, PROBE_TIMEOUT)
    try:
        info = client.server_info()
    except IncusError as error:
        entry["problem"] = error.message
        return entry
    entry["info"] = info
    if info.get("auth") != "trusted":
        entry["problem"] = "It does not trust this console."
    elif (info.get("environment") or {}).get("server_clustered"):
        entry["problem"] = "It is already part of a cluster."
    else:
        version = (info.get("environment") or {}).get("server_version") or ""
        # Incus refuses a member whose version differs from the cluster's.
        if cluster_version and version and version != cluster_version:
            entry["problem"] = (
                f"It runs Incus {version} and the cluster runs {cluster_version}; "
                "members must run the same version."
            )
    return entry


def _candidates() -> list[dict]:
    """The other registered servers, each with why it cannot join (None if it can)."""
    member_urls = {(m.get("url") or "").rstrip("/") for m in cluster_members()}
    version = _cluster_version()
    return [
        _probe(row, version)
        for row in servers.all_servers()
        if row["id"] != g.sid
        and not (row["kind"] == "https" and row["address"].rstrip("/") in member_urls)
    ]


def _candidate(server_id) -> tuple:
    """(registered server, its probe) for a server that can join; 404 otherwise."""
    try:
        row = servers.get(int(server_id))
    except (TypeError, ValueError):
        abort(404)
    if row is None or row["id"] == g.sid:
        abort(404)
    return row, _probe(row, _cluster_version())


@bp.route("/cluster/join", methods=["GET", "POST"])
def cluster_join():
    if not cluster_members():
        abort(404)
    chosen = request.values.get("server")
    if not chosen:
        return render_template("cluster/join_pick.html", candidates=_candidates())
    row, probe = _candidate(chosen)
    member_config = _member_config()
    env = probe["info"].get("environment") or {}
    values = {
        "name": env.get("server_name") or "",
        "address": _suggested_address(probe["info"], row) if probe["info"] else "",
        "config": [item.get("value", "") for item in member_config],
    }
    if request.method == "POST" and probe["problem"] is None:
        form = request.form
        values = {
            "name": form.get("name", "").strip(),
            "address": form.get("address", "").strip(),
            "config": [form.get(f"config-{i}", "").strip() for i in range(len(member_config))],
        }
        joined = _join(row, values, member_config)
        if joined:
            return redirect(url_for("srv.cluster"))
    return render_template(
        "cluster/join.html",
        target=row,
        probe=probe,
        values=values,
        member_config=member_config,
    )


def _join(row, values: dict, member_config: list[dict]) -> bool:
    """Bring a registered server into this cluster: a token from here, used over there."""
    name = values["name"]
    if not _new_member_name(name):
        return False
    address = listen_address(values["address"])
    if address is None:
        flash(ADDRESS_RULE, "error")
        return False
    pairs = zip(member_config, values["config"], strict=True)
    answers = [{**item, "value": value} for item, value in pairs]
    try:
        token_op = g.incus.add_cluster_member(name).get("metadata") or {}
        certificate = (g.incus.server_info().get("environment") or {}).get("certificate", "")
    except IncusError as error:
        flash(error.message, "error")
        return False
    meta = token_op.get("metadata") or {}
    fingerprint = normalize_fingerprint(meta.get("fingerprint"))
    addresses = meta.get("addresses") or []
    # Incus does not read the token for the address and certificate (incus
    # admin init does): without them, the server would start a cluster of its own.
    if not addresses or not fingerprint or _pem_fingerprint(certificate) != fingerprint:
        _drop_token(token_op)
        flash("The cluster's certificate does not match its join token.", "error")
        return False
    payload = {
        "server_name": name,
        "enabled": True,
        "server_address": address,
        "cluster_address": addresses[0],
        "cluster_certificate": certificate,
        "cluster_token": join_token(token_op),
        "member_config": answers,
    }
    joiner = servers.client_for(row)
    try:
        response = joiner.update_cluster(payload)
        finished = _wait_for_join(joiner, response)
    except IncusError as error:
        _drop_token(token_op)
        flash(f"{row['name']} could not join: {error.message}", "error")
        return False
    if row["kind"] == "https":
        servers.set_fingerprint(row["id"], fingerprint)
    if not finished:
        flash(
            f"{row['name']} is joining the cluster. It shows up here as {name} when it is done.",
            "info",
        )
    elif any(m["server_name"] == name for m in g.incus.cluster_members()):
        flash(f"{row['name']} joined the cluster as {name}.", "success")
    else:
        flash(
            f"{row['name']} says it is done, but the cluster does not list {name}. "
            "Check its Cluster page.",
            "error",
        )
    return True


def _pem_fingerprint(pem: str) -> str | None:
    try:
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    except (ValueError, TypeError):
        return None


def _wait_for_join(joiner, response) -> bool:
    """True when it finished. Part way through, the server takes on the
    cluster's certificate, so losing touch with it means it is still going."""
    try:
        return joiner.wait(response, current_app.config["ACTION_WAIT"])
    except IncusConnectionError:
        return False


def _drop_token(operation: dict) -> None:
    try:
        g.incus.cancel_operation(operation["id"])
    except (IncusError, KeyError):
        pass


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
    versions = _versions([found])
    return render_template(
        "cluster/member.html",
        member=found,
        version=versions.get(member, ""),
        database=database_role(found),
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
        new_name = form.get("name", member).strip()
        chosen = [name for name in form.getlist("groups") if name in groups]
        automatic = [r for r in found.get("roles") or [] if r not in ASSIGNABLE_ROLES]
        config = dict(found.get("config") or {})
        scheduler = form.get("scheduler", "")
        if scheduler in SCHEDULER_CHOICES and scheduler != "all":
            config["scheduler.instance"] = scheduler
        else:
            config.pop("scheduler.instance", None)
        payload = {
            "description": form.get("description", "").strip(),
            "failure_domain": form.get("failure_domain", "").strip() or "default",
            "roles": automatic + [r for r in form.getlist("roles") if r in ASSIGNABLE_ROLES],
            "groups": chosen,
            "config": config,
        }
        if not chosen:
            flash("A member belongs to at least one group.", "error")
        elif new_name != member and not _new_member_name(new_name):
            pass
        elif run(
            lambda: _save_member(member, new_name, payload),
            f"{new_name} updated." if new_name == member else f"{member} is now {new_name}.",
        ):
            return redirect(url_for("srv.cluster_member", member=new_name))
        found = {**found, **payload}
    return render_template(
        "cluster/member_edit.html", member=found, groups=groups, roles=ASSIGNABLE_ROLES
    )


def _save_member(member: str, new_name: str, payload: dict) -> dict:
    response = g.incus.update_cluster_member(member, payload)
    if new_name != member:
        response = g.incus.rename_cluster_member(member, new_name)
    return response


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
    # Forcing is for a member that is gone: Incus forgets it, and the
    # instances and volumes it held, without asking it.
    force = request.form.get("force") == "1"
    if force and found.get("status") != "Offline":
        flash(f"{member} is {found.get('status', '').lower()}: remove it normally.", "error")
        return redirect(url_for("srv.cluster_member", member=member))
    removed = f"{member} {'forgotten by' if force else 'removed from'} the cluster."
    if run(lambda: g.incus.remove_cluster_member(member, force=force), removed):
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
