"""Per-user restricted certificates (server mode, remote servers).

Each non-admin user gets their own client certificate for each remote server they
have access to. The console adds it to that server's trust store as a restricted
certificate listing the user's projects, then sends that user's requests with it.
Incus then refuses anything outside those projects, and refuses changes to project
settings and server-wide configuration, whatever the console does.

Console admins, local-socket servers and desktop mode use the console's own
certificate or socket. The console's own certificate manages the trust store, so
it must be unrestricted.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from flask import current_app

from . import access
from .certs import fingerprint, generate_self_signed
from .db import get_db
from .incus import IncusClient, IncusError

NAME_PREFIX = "incus-console/"


def applies(server) -> bool:
    config = current_app.config
    return bool(
        config["SERVER_MODE"] and config["PER_USER_CERTIFICATES"] and server["kind"] == "https"
    )


def _paths(user_id: int, server_id: int) -> tuple[Path, Path]:
    folder = Path(current_app.config["DATA_DIR"]) / "user-certs"
    folder.mkdir(mode=0o700, exist_ok=True)
    stem = folder / f"u{user_id}-s{server_id}"
    return stem.with_suffix(".crt"), stem.with_suffix(".key")


def _user(user_id: int):
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _row(user_id: int, server_id: int):
    return (
        get_db()
        .execute(
            "SELECT * FROM user_certs WHERE user_id = ? AND server_id = ?", (user_id, server_id)
        )
        .fetchone()
    )


def _admin_client(server) -> IncusClient:
    from .servers import client_for

    return client_for(server)


def _wanted(user, server, admin: IncusClient) -> list[str] | None:
    """Projects the user's certificate should list, or None for no certificate."""
    if user is None or user["is_admin"] or user["disabled"]:
        return None
    grants = {row["project"] for row in access.grants_for(user["id"], server["id"])}
    if not grants:
        return None
    if access.ALL_PROJECTS in grants:
        return sorted(p["name"] for p in admin.projects())
    return sorted(grants)


def _der_base64(crt: Path) -> str:
    cert = x509.load_pem_x509_certificate(crt.read_bytes())
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


def sync(server, user_id: int) -> None:
    """Make the server's trust store match the user's access. Raises IncusError."""
    if not applies(server):
        return
    user = _user(user_id)
    admin = _admin_client(server)
    projects = _wanted(user, server, admin)
    row = _row(user_id, server["id"])
    if projects is None:
        if row is not None:
            remove(server, user_id)
        return
    db = get_db()
    crt, key = _paths(user_id, server["id"])
    if row is not None and crt.exists():
        if json.loads(row["projects"]) != projects:
            current = admin.certificate(row["fingerprint"])
            admin.update_certificate(
                row["fingerprint"],
                {
                    "name": current.get("name") or NAME_PREFIX + user["username"],
                    "type": "client",
                    "restricted": True,
                    "projects": projects,
                    "certificate": current.get("certificate", ""),
                },
            )
        db.execute(
            "UPDATE user_certs SET projects = ?, synced_at = ? WHERE user_id = ? AND server_id = ?",
            (json.dumps(projects), time.time(), user_id, server["id"]),
        )
        db.commit()
        return
    if row is not None:  # the files went missing; start over
        remove(server, user_id)
    generate_self_signed(NAME_PREFIX + user["username"], crt, key)
    try:
        admin.add_certificate(
            {
                "name": NAME_PREFIX + user["username"],
                "type": "client",
                "restricted": True,
                "projects": projects,
                "certificate": _der_base64(crt),
            }
        )
    except IncusError:
        crt.unlink(missing_ok=True)
        key.unlink(missing_ok=True)
        raise
    db.execute(
        "INSERT INTO user_certs (user_id, server_id, fingerprint, projects, synced_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, server["id"], fingerprint(crt), json.dumps(projects), time.time()),
    )
    db.commit()


def remove(server, user_id: int) -> None:
    """Take the user's certificate out of the server's trust store and forget it."""
    row = _row(user_id, server["id"])
    if row is None:
        return
    try:
        _admin_client(server).delete_certificate(row["fingerprint"])
    except IncusError as error:
        if error.status != 404:
            raise
    crt, key = _paths(user_id, server["id"])
    for file in (crt, key):
        if file.exists():
            os.unlink(file)
    db = get_db()
    db.execute(
        "DELETE FROM user_certs WHERE user_id = ? AND server_id = ?", (user_id, server["id"])
    )
    db.commit()


def sync_all(user_id: int) -> list[str]:
    """Sync one user on every server; returns problems as messages."""
    from .servers import all_servers

    problems = []
    for server in all_servers():
        try:
            sync(server, user_id)
        except IncusError as error:
            problems.append(f"{server['name']}: {error.message}")
    return problems


def sync_server(server) -> list[str]:
    """Sync every user who has access to one server (after projects change)."""
    problems = []
    rows = get_db().execute(
        "SELECT DISTINCT user_id FROM grants WHERE server_id = ? "
        "UNION SELECT user_id FROM user_certs WHERE server_id = ?",
        (server["id"], server["id"]),
    )
    for row in rows.fetchall():
        try:
            sync(server, row["user_id"])
        except IncusError as error:
            problems.append(error.message)
    return problems


def forget_server(server) -> None:
    """Best effort, before a server is removed from the console."""
    for row in (
        get_db()
        .execute("SELECT user_id FROM user_certs WHERE server_id = ?", (server["id"],))
        .fetchall()
    ):
        try:
            remove(server, row["user_id"])
        except IncusError:
            continue


def client(server, user, project: str) -> IncusClient | None:
    """The user's own client for this server, or None to use the console's."""
    if not applies(server) or user is None or user["is_admin"]:
        return None
    row = _row(user["id"], server["id"])
    crt, key = _paths(user["id"], server["id"])
    stale = row is None or not crt.exists()
    if not stale:
        listed = json.loads(row["projects"])
        interval = current_app.config["CERT_SYNC_INTERVAL"]
        all_projects = access.ALL_PROJECTS in {
            g["project"] for g in access.grants_for(user["id"], server["id"])
        }
        stale = all_projects and (
            project not in listed or time.time() - row["synced_at"] > interval
        )
    if stale:
        sync(server, user["id"])
        if _row(user["id"], server["id"]) is None:
            raise IncusError(403, "You do not have access to this server.")
    return IncusClient(
        url=server["address"],
        cert=str(crt),
        key=str(key),
        fingerprint=server["fingerprint"],
        project=project,
        timeout=current_app.config["INCUS_TIMEOUT"],
    )
