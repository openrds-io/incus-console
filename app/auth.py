"""Access control.

Server mode: users sign in with a username and password.
Desktop mode: no login screen; the desktop runtime proves itself with DESKTOP_KEY.
Both modes: every state-changing request must carry the session's CSRF token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

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
from flask.cli import with_appcontext
from werkzeug.security import check_password_hash, generate_password_hash

from . import audit, totp
from .db import get_db

bp = Blueprint("auth", __name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DESKTOP_USER = {"id": 0, "username": "desktop", "is_admin": 1}
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
            # The session remembers this launch's key (hashed), so a cookie from an
            # earlier launch does not open the console.
            proof = hashlib.sha256(str(key).encode()).hexdigest()
            supplied = request.args.get("key")
            if supplied and hmac.compare_digest(str(supplied), str(key)):
                session["desktop"] = proof
            if not hmac.compare_digest(str(session.get("desktop", "")), proof):
                abort(401)
        g.user = DESKTOP_USER
        return None

    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = (
            get_db()
            .execute(
                "SELECT id, username, is_admin, session_epoch, "
                "totp_secret IS NOT NULL AS two_factor FROM users "
                "WHERE id = ? AND disabled = 0",
                (user_id,),
            )
            .fetchone()
        )
        # A session from before a password change or "sign out everywhere" is over.
        if g.user is None or session.get("epoch", 0) != g.user["session_epoch"]:
            g.user = None
            session.clear()
    if g.user is None and request.endpoint not in SIGNING_IN:
        target = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("auth.login", next=target))
    if (
        g.user is not None
        and current_app.config["REQUIRE_TWO_FACTOR"]
        and not g.user["two_factor"]
        and request.endpoint not in SETTING_UP
    ):
        flash("Set up two-step sign-in to carry on: this console requires it.", "error")
        return redirect(url_for("account.two_factor"))
    return None


SIGNING_IN = ("auth.login", "auth.login_code")
# What a user may open before they have the second step a console insists on.
SETTING_UP = (*SIGNING_IN, "auth.logout", "account.two_factor")
# How long someone has for the second step after their password.
PENDING_SECONDS = 5 * 60


FAILED = "Failed log in"
LOCKED = "Too many failed attempts. Try again later, or ask a console admin to unlock the account."
ADDRESS_LOCKED = "Too many failed attempts from your network address. Try again later."


def is_locked(username: str) -> bool:
    """Has this username had too many failed sign-ins lately?

    Counted from the audit log, for names that are not users too, so the answer
    does not tell an attacker which usernames exist.
    """
    most = int(current_app.config["LOGIN_MAX_FAILURES"] or 0)
    if most <= 0 or not username:
        return False
    db = get_db()
    user = db.execute(
        "SELECT failures_reset_at FROM users WHERE username = ?", (username,)
    ).fetchone()
    since = (user["failures_reset_at"] if user else None) or ""
    window = f"-{int(current_app.config['LOGIN_LOCKOUT_SECONDS'])} seconds"
    failures = db.execute(
        "SELECT COUNT(*) FROM audit WHERE action = ? AND username = ? "
        "AND at >= datetime('now', ?) AND at > ?",
        (FAILED, username, window, since),
    ).fetchone()[0]
    return failures >= most


def address_is_locked() -> bool:
    """Have too many sign-ins failed lately from where this request comes from?

    A successful sign-in does not reset this: one good password among the
    guesses should not buy an attacker another round.
    """
    most = int(current_app.config["LOGIN_MAX_FAILURES_PER_ADDRESS"] or 0)
    if most <= 0 or not request.remote_addr:
        return False
    window = f"-{int(current_app.config['LOGIN_LOCKOUT_SECONDS'])} seconds"
    failures = (
        get_db()
        .execute(
            "SELECT COUNT(*) FROM audit WHERE action = ? AND address = ? "
            "AND at >= datetime('now', ?)",
            (FAILED, request.remote_addr, window),
        )
        .fetchone()[0]
    )
    return failures >= most


def forget_failures(user_id: int) -> None:
    """Start counting failed sign-ins afresh (after a sign-in, a reset, or an unlock)."""
    db = get_db()
    # Failures are compared with "later than this second", so move just past now.
    db.execute(
        "UPDATE users SET failures_reset_at = datetime('now', '+1 second') WHERE id = ?",
        (user_id,),
    )
    db.commit()


def end_sessions(user_id: int) -> int:
    """Sign the user out everywhere; returns the number new sessions must carry."""
    db = get_db()
    db.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = ?", (user_id,))
    db.commit()
    return db.execute("SELECT session_epoch FROM users WHERE id = ?", (user_id,)).fetchone()[0]


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not current_app.config["SERVER_MODE"]:
        return redirect(url_for("servers.index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        valid = check_password_hash(user["password_hash"] if user else DUMMY_HASH, password)
        if address_is_locked():
            audit.record(username or "-", "Refused log in (address locked)", status=429)
            flash(ADDRESS_LOCKED, "error")
        elif is_locked(username):
            # Not counted as another failure, or the lock would never run out.
            audit.record(username, "Refused log in (locked)", status=429)
            flash(LOCKED, "error")
        elif user and valid and not user["disabled"]:
            target = _safe_next(request.form.get("next"))
            if user["totp_secret"]:
                # The password was right; the session says so, and no more, until
                # the code is too.
                session.clear()
                session["pending"] = {"id": user["id"], "next": target, "at": int(time.time())}
                return redirect(url_for("auth.login_code"))
            return _signed_in(user, target)
        else:
            audit.record(username or "-", FAILED, status=401)
            flash("Wrong username or password.", "error")
    return render_template("login.html", next=_safe_next(request.values.get("next")))


def _signed_in(user, target: str | None, how: str | None = None):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["epoch"] = user["session_epoch"]
    forget_failures(user["id"])
    audit.record(user["username"], "Logged in", status=200, outcome=how)
    return redirect(target or url_for("servers.home"))


@bp.route("/login/code", methods=["GET", "POST"])
def login_code():
    """The second step: the code from the user's authenticator app, or a recovery code."""
    if not current_app.config["SERVER_MODE"]:
        return redirect(url_for("servers.index"))
    pending = session.get("pending") or {}
    user = None
    if time.time() - pending.get("at", 0) <= PENDING_SECONDS:
        user = (
            get_db()
            .execute(
                "SELECT * FROM users WHERE id = ? AND disabled = 0 AND totp_secret IS NOT NULL",
                (pending.get("id"),),
            )
            .fetchone()
        )
    if user is None:
        session.pop("pending", None)
        if pending:
            flash("That took too long. Log in again.", "error")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if address_is_locked() or is_locked(user["username"]):
            session.pop("pending", None)
            audit.record(user["username"], "Refused log in (locked)", status=429)
            flash(LOCKED, "error")
            return redirect(url_for("auth.login"))
        kind = totp.check(user, request.form.get("code", ""))
        if kind:
            how = "With a recovery code" if kind == "recovery" else None
            response = _signed_in(user, _safe_next(pending.get("next")), how)
            if kind == "recovery":
                left = totp.recovery_codes_left(user["id"])
                flash(
                    f"You used a recovery code; {left} left. "
                    "Make new ones under Account if you are running low.",
                    "success",
                )
            return response
        # Counts towards the lockout like a wrong password.
        audit.record(user["username"], FAILED, status=401, outcome="Wrong code")
        flash("That code is not right. Codes change every 30 seconds.", "error")
    return render_template("login_code.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


MIN_PASSWORD = 10


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Use a password of at least {MIN_PASSWORD} characters.")


@click.command("create-user")
@with_appcontext
@click.argument("username")
@click.password_option()
@click.option("--admin", is_flag=True, help="Make the user a console admin.")
def create_user(username: str, password: str, admin: bool) -> None:
    """Create a console user, or reset the password of an existing one.

    The first user is always an admin, so a new install can be set up.
    """
    try:
        check_password(password)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    db = get_db()
    if not db.execute("SELECT 1 FROM users WHERE is_admin = 1 AND disabled = 0").fetchone():
        admin = True
    db.execute(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash, "
        "is_admin = MAX(is_admin, excluded.is_admin), disabled = 0",
        (username, generate_password_hash(password), int(admin)),
    )
    db.commit()
    click.echo(f"User {username} saved{' as an admin' if admin else ''}.")


@click.command("two-factor-off")
@with_appcontext
@click.argument("username")
def two_factor_off(username: str) -> None:
    """Turn two-step sign-in off for a user who lost their phone and their codes."""
    user = get_db().execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if user is None:
        raise click.ClickException(f"There is no user {username}.")
    totp.disable(user["id"])
    click.echo(f"Two-step sign-in is off for {username}; their password still works.")


@click.command("unlock")
@with_appcontext
@click.argument("username")
def unlock(username: str) -> None:
    """Let a user sign in again after too many failed attempts locked the name."""
    user = get_db().execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if user is None:
        raise click.ClickException(f"There is no user {username}.")
    forget_failures(user["id"])
    click.echo(f"{username} can sign in again.")


def init_app(app) -> None:
    app.register_blueprint(bp)
    app.cli.add_command(create_user)
    app.cli.add_command(unlock)
    app.cli.add_command(two_factor_off)
    app.jinja_env.globals["csrf_token"] = csrf_token
