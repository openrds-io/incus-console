"""The registry of Incus servers this console can manage."""

from __future__ import annotations

import glob
import os
import sqlite3
from pathlib import Path

from flask import current_app

from .certs import ensure_client_cert
from .db import get_db
from .incus import IncusClient, IncusError


def all_servers() -> list[sqlite3.Row]:
    return get_db().execute("SELECT * FROM servers ORDER BY name").fetchall()


def get(server_id: int) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()


def add(name: str, kind: str, address: str, fingerprint: str | None = None) -> int:
    name, address = name.strip(), address.strip()
    if not name or not address:
        raise IncusError(400, "A server needs a name and an address.")
    if kind == "unix":
        address = str(Path(address).expanduser())
    elif kind == "https":
        if not address.startswith("https://"):
            raise IncusError(400, "Remote servers need an https:// address.")
    else:
        raise IncusError(400, "Unknown server type.")
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO servers (name, kind, address, fingerprint) VALUES (?, ?, ?, ?)",
            (name, kind, address, fingerprint or None),
        )
    except sqlite3.IntegrityError as exc:
        raise IncusError(400, f"A server named {name!r} already exists.") from exc
    db.commit()
    return cursor.lastrowid


def set_fingerprint(server_id: int, fingerprint: str) -> None:
    """A server that joins a cluster answers with the cluster's certificate from then on."""
    db = get_db()
    db.execute("UPDATE servers SET fingerprint = ? WHERE id = ?", (fingerprint, server_id))
    db.commit()


def delete(server_id: int) -> None:
    db = get_db()
    db.execute("DELETE FROM servers WHERE id = ?", (server_id,))
    db.commit()


def client_for(server: sqlite3.Row, project: str = "default") -> IncusClient:
    timeout = current_app.config["INCUS_TIMEOUT"]
    if server["kind"] == "unix":
        return IncusClient(socket_path=server["address"], project=project, timeout=timeout)
    cert, key = ensure_client_cert(current_app.config["DATA_DIR"])
    return IncusClient(
        url=server["address"],
        cert=cert,
        key=key,
        fingerprint=server["fingerprint"],
        project=project,
        timeout=timeout,
    )


def detected_sockets() -> list[dict]:
    """Local Incus sockets that exist, are accessible and are not registered yet.

    Each entry is ``{"path": ..., "name": ...}``; the name is a suggestion for the form.
    """
    rows = all_servers()
    known = {row["address"] for row in rows if row["kind"] == "unix"}
    names = {row["name"] for row in rows}
    found: dict[str, dict] = {}
    for pattern in current_app.config["LOCAL_SOCKETS"]:
        for candidate in sorted(glob.glob(str(Path(pattern).expanduser()))):
            if candidate in known or candidate in found:
                continue
            if not os.access(candidate, os.R_OK | os.W_OK):
                continue
            parent = Path(candidate).parent
            name = f"Colima {parent.name}" if parent.parent.name == ".colima" else "Local"
            if name in names:
                name = f"{name} ({candidate})"
            names.add(name)
            found[candidate] = {"path": candidate, "name": name}
    return list(found.values())
