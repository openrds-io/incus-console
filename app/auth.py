"""Access control.

Server mode: users sign in with a username and password.
Desktop mode: no login screen; the desktop runtime proves itself with DESKTOP_KEY.
Both modes: every state-changing request must carry the session's CSRF token.
"""

from __future__ import annotations

import hmac
import secrets

import click
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db

bp = Blueprint("auth", __name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DESKTOP_USER = {"id": 0, "username": "desktop"}
# Checked when the username is unknown so both failures take similar time.
DUMMY_HASH = generate_password_hash("incus-console")


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _check_csrf() -> None:
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("csrf") or ""
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, "The form expired or was not sent from this site. Reload and try again.")


def _safe_next(target: str | None) -> str | None:
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return None


@bp.before_app_request
def gate():
    if request.endpoint in (None, "static"):
        return None
    if request.method not in SAFE_METHODS:
        _check_csrf()

    if not current_app.config["SERVER_MODE"]:
        key = current_app.config.get("DESKTOP_KEY")
        if key:
            supplied = request.args.get("key")
            if supplied and hmac.compare_digest(str(supplied), str(key)):
                session["desktop"] = True
            if not session.get("desktop"):
                abort(401)
        g.user = DESKTOP_USER
        return None

    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = (
            get_db().execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        )
    if g.user is None and request.endpoint != "auth.login":
        target = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("auth.login", next=target))
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not current_app.config["SERVER_MODE"]:
        return redirect(url_for("servers.index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        valid = check_password_hash(user["password_hash"] if user else DUMMY_HASH, password)
        if user and valid:
            target = _safe_next(request.form.get("next"))
            session.clear()
            session["user_id"] = user["id"]
            return redirect(target or url_for("servers.index"))
        flash("Wrong username or password.", "error")
    return render_template("login.html", next=_safe_next(request.values.get("next")))


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@click.command("create-user")
@click.argument("username")
@click.password_option()
def create_user(username: str, password: str) -> None:
    """Create a console user, or reset the password of an existing one."""
    if len(password) < 8:
        raise click.ClickException("Use a password of at least 8 characters.")
    db = get_db()
    db.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?) "
        "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
        (username, generate_password_hash(password)),
    )
    db.commit()
    click.echo(f"User {username} saved.")


def init_app(app) -> None:
    app.register_blueprint(bp)
    app.cli.add_command(create_user)
    app.jinja_env.globals["csrf_token"] = csrf_token
