"""The registry of Incus servers this console can manage."""

from __future__ import annotations

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


def detected_sockets() -> list[str]:
    """Local Incus sockets that exist, are accessible and are not registered yet."""
    known = {row["address"] for row in all_servers() if row["kind"] == "unix"}
    found = []
    for candidate in current_app.config["LOCAL_SOCKETS"]:
        candidate = str(Path(candidate).expanduser())
        usable = os.access(candidate, os.R_OK | os.W_OK)
        if candidate not in known and candidate not in found and usable:
            found.append(candidate)
    return found
