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
    is_admin INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Sessions carry this number; raising it signs the user out everywhere.
    session_epoch INTEGER NOT NULL DEFAULT 0,
    -- Failed sign-ins before this moment no longer count towards a lockout.
    failures_reset_at TEXT,
    -- Two-step sign-in is on when there is a secret; totp_step is the time step of
    -- the last code taken, so a code cannot be used twice.
    totp_secret TEXT,
    totp_step INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recovery_codes (
    -- One-time codes for signing in without the authenticator app; kept hashed.
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recovery_codes_user ON recovery_codes (user_id);
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Roles enforced by Incus (app/authz.py): on or off, and what was last sent.
    enforce_roles INTEGER NOT NULL DEFAULT 0,
    scriptlet_hash TEXT
);
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    server_id INTEGER NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'project_admin')),
    UNIQUE (user_id, server_id, project)
);
CREATE TABLE IF NOT EXISTS instance_grants (
    -- A role on one instance, for a user with no role (or a lesser one) in its project.
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    server_id INTEGER NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    instance TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'operator')),
    UNIQUE (user_id, server_id, project, instance)
);
CREATE TABLE IF NOT EXISTS group_instance_grants (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES user_groups (id) ON DELETE CASCADE,
    server_id INTEGER NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    instance TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'operator')),
    UNIQUE (group_id, server_id, project, instance)
);
CREATE TABLE IF NOT EXISTS user_groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    -- The identity provider's name for this group, for mapping at sign-in (unused yet).
    external_name TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES user_groups (id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS group_members_user ON group_members (user_id);
CREATE TABLE IF NOT EXISTS group_grants (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES user_groups (id) ON DELETE CASCADE,
    server_id INTEGER NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'project_admin')),
    UNIQUE (group_id, server_id, project)
);
CREATE TABLE IF NOT EXISTS user_certs (
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    server_id INTEGER NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    projects TEXT NOT NULL,
    synced_at REAL NOT NULL,
    PRIMARY KEY (user_id, server_id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    username TEXT NOT NULL,
    server_id INTEGER,
    project TEXT,
    action TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER NOT NULL,
    outcome TEXT,
    -- Where the request came from; failed sign-ins are counted per address too.
    address TEXT
);
CREATE INDEX IF NOT EXISTS audit_at ON audit (at);
"""


def path_for(data_dir: str) -> Path:
    return Path(data_dir) / "incus-console.db"


def connect(data_dir: str) -> sqlite3.Connection:
    db = sqlite3.connect(path_for(data_dir), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATA_DIR"])
    return g.db


def _close(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate(db: sqlite3.Connection) -> None:
    columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    if "is_admin" not in columns:
        # Before roles existed every user could do everything; keep it that way
        # for them until an admin says otherwise.
        db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE users SET is_admin = 1")
        db.commit()
    if "session_epoch" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE users ADD COLUMN failures_reset_at TEXT")
        db.commit()
    if "totp_secret" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
        db.execute("ALTER TABLE users ADD COLUMN totp_step INTEGER NOT NULL DEFAULT 0")
        db.commit()
    audit = {row["name"] for row in db.execute("PRAGMA table_info(audit)")}
    if audit and "address" not in audit:
        db.execute("ALTER TABLE audit ADD COLUMN address TEXT")
        db.commit()
    servers = {row["name"] for row in db.execute("PRAGMA table_info(servers)")}
    if servers and "enforce_roles" not in servers:
        db.execute("ALTER TABLE servers ADD COLUMN enforce_roles INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE servers ADD COLUMN scriptlet_hash TEXT")
        db.commit()


def init_app(app) -> None:
    app.teardown_appcontext(_close)
    with app.app_context():
        db = get_db()
        # Older databases: give users their new columns before the schema indexes them.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'users'").fetchone():
            _migrate(db)
        db.executescript(SCHEMA)
        keep = int(app.config.get("AUDIT_KEEP_DAYS") or 0)
        if keep > 0:
            db.execute("DELETE FROM audit WHERE at < datetime('now', ?)", (f"-{keep} days",))
            db.commit()
