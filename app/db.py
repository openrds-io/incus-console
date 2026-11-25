"""SQLite storage for console users and registered Incus servers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tracked_operations (
    id INTEGER PRIMARY KEY,
    server_id INTEGER NOT NULL,
    project TEXT NOT NULL,
    op_id TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Running',
    error TEXT,
    seen INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('unix', 'https')),
    address TEXT NOT NULL,
    fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def path_for(data_dir: str) -> Path:
    return Path(data_dir) / "incus-console.db"


def connect(data_dir: str) -> sqlite3.Connection:
    db = sqlite3.connect(path_for(data_dir), timeout=10)
    db.row_factory = sqlite3.Row
    return db


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATA_DIR"])
    return g.db


def _close(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app) -> None:
    app.teardown_appcontext(_close)
    with app.app_context():
        get_db().executescript(SCHEMA)
