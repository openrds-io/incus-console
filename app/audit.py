"""Audit log (server mode): who changed what, where, and how it went.

Every request that changes something is recorded after it runs, with the message
the page showed. Sign-ins, failed sign-ins and opened terminals are recorded too.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from flask import current_app, g, request, session

from .db import get_db

# GET requests worth recording.
RECORDED_GETS = {"srv.instance_terminal_ws": "Opened terminal"}


forwarder = logging.getLogger("incus_console.audit")
forwarder.propagate = False
_destination: logging.Handler | None = None


def _forward_to(where: str) -> None:
    """Point the forwarder at AUDIT_FORWARD's destination."""
    global _destination
    if _destination is not None:
        forwarder.removeHandler(_destination)
        _destination.close()
        _destination = None
    handler: logging.Handler | None = None
    if where == "log":
        handler = logging.StreamHandler(sys.stderr)
    elif where == "syslog":
        try:
            handler = logging.handlers.SysLogHandler(
                address="/dev/log", facility=logging.handlers.SysLogHandler.LOG_AUTHPRIV
            )
        except OSError as error:
            # Not worth refusing to start over; say so where an admin will look.
            print(f"incus-console: AUDIT_FORWARD=syslog but /dev/log: {error}", file=sys.stderr)
    elif where:
        raise ValueError(f'AUDIT_FORWARD must be "", "log" or "syslog", not {where!r}')
    if handler is not None:
        handler.setFormatter(logging.Formatter("incus-console audit: %(message)s"))
        forwarder.addHandler(handler)
        forwarder.setLevel(logging.INFO)
        _destination = handler


def _quoted(value) -> str:
    """A value for a key=value log line: quoted when it has spaces, on one line."""
    text = "-" if value in (None, "") else " ".join(str(value).split())
    if re_plain(text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def re_plain(text: str) -> bool:
    return bool(text) and all(c.isalnum() or c in "-_./:@%+," for c in text)


def line(username, server, project, action, path, status, outcome) -> str:
    """One audit entry as a line for a log: key=value pairs, in a fixed order."""
    pairs = (
        ("user", username),
        ("server", server),
        ("project", project),
        ("action", action),
        ("path", path),
        ("status", status),
        ("outcome", outcome),
    )
    return " ".join(f"{key}={_quoted(value)}" for key, value in pairs)


def record(
    username: str,
    action: str,
    *,
    status: int,
    server_id: int | None = None,
    project: str | None = None,
    outcome: str | None = None,
) -> None:
    if not current_app.config["SERVER_MODE"]:
        return
    db = get_db()
    db.execute(
        "INSERT INTO audit (username, server_id, project, action, path, status, outcome) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, server_id, project, action, request.path, status, outcome),
    )
    db.commit()
    if _destination is not None:
        server = None
        if server_id is not None:
            row = db.execute("SELECT name FROM servers WHERE id = ?", (server_id,)).fetchone()
            server = row["name"] if row else server_id
        forwarder.info(line(username, server, project, action, request.path, status, outcome))


def _action() -> str:
    endpoint = request.endpoint or ""
    if endpoint in RECORDED_GETS:
        return RECORDED_GETS[endpoint]
    words = endpoint.split(".", 1)[-1].replace("_", " ")
    detail = request.form.get("action")
    return f"{words}: {detail}" if detail else words


def _after(response):
    endpoint = request.endpoint or ""
    if endpoint in ("auth.login", "static") or not endpoint:
        return response
    if request.method in ("GET", "HEAD", "OPTIONS") and endpoint not in RECORDED_GETS:
        return response
    user = g.get("user")
    if not user:
        return response
    flashes = session.get("_flashes") or []
    outcome = flashes[-1][1] if flashes else None
    if response.status_code == 403:
        outcome = "Not allowed"
    record(
        user["username"],
        _action(),
        status=response.status_code,
        server_id=g.get("sid"),
        project=g.get("project"),
        outcome=outcome,
    )
    return response


def recent(
    limit: int = 200,
    username: str | None = None,
    *,
    server_id: int | None = None,
    action: str | None = None,
    since: str | None = None,
    until: str | None = None,
):
    """Entries, newest first. ``since`` and ``until`` are YYYY-MM-DD days (UTC), inclusive."""
    sql = (
        "SELECT audit.*, servers.name AS server_name FROM audit "
        "LEFT JOIN servers ON servers.id = audit.server_id"
    )
    where, args = [], []
    if username:
        where.append("audit.username = ?")
        args.append(username)
    if server_id is not None:
        where.append("audit.server_id = ?")
        args.append(server_id)
    if action:
        where.append("audit.action LIKE ? ESCAPE '!'")
        escaped = action.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        args.append(f"%{escaped}%")
    if since:
        where.append("audit.at >= ?")
        args.append(since)
    if until:
        where.append("audit.at < date(?, '+1 day')")
        args.append(until)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY audit.id DESC LIMIT ?"
    args.append(limit)
    return get_db().execute(sql, args).fetchall()


def init_app(app) -> None:
    _forward_to(app.config.get("AUDIT_FORWARD") or "" if app.config["SERVER_MODE"] else "")
    app.after_request(_after)
