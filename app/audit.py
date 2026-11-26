"""Audit log (server mode): who changed what, where, and how it went.

Every request that changes something is recorded after it runs, with the message
the page showed. Sign-ins, failed sign-ins and opened terminals are recorded too.
"""

from __future__ import annotations

from flask import current_app, g, request, session

from .db import get_db

# GET requests worth recording.
RECORDED_GETS = {"srv.instance_terminal_ws": "Opened terminal"}


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
    app.after_request(_after)
