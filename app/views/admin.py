"""Server mode: console users, their access, the audit log, and each user's own account."""

from __future__ import annotations

import re

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .. import access, audit, servers, user_certs
from ..auth import check_password
from ..db import get_db
from ..incus import IncusError

bp = Blueprint("admin", __name__, url_prefix="/admin")
account_bp = Blueprint("account", __name__)

USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
USERNAME_RULE = (
    "Use up to 64 letters, digits, dots, dashes, underscores or @, starting with a letter or digit."
)


@bp.before_request
def _admins_only():
    if not current_app.config["SERVER_MODE"]:
        abort(404)
    if not access.is_admin():
        abort(403)


@account_bp.before_request
def _server_mode_only():
    if not current_app.config["SERVER_MODE"]:
        abort(404)


def _user(user_id: int):
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    return user


def _other_active_admins(user_id: int) -> int:
    row = (
        get_db()
        .execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND disabled = 0 AND id != ?", (user_id,)
        )
        .fetchone()
    )
    return row[0]


def _project_names() -> list[str]:
    """Project names across all servers, for the grant form's suggestions."""
    names: set[str] = set()
    for server in servers.all_servers():
        try:
            names.update(p["name"] for p in servers.client_for(server).projects())
        except IncusError:
            continue
    return sorted(names)


def _report(problems: list[str]) -> None:
    if problems:
        flash(
            "Saved here, but the Incus trust store was not updated: " + "; ".join(problems),
            "error",
        )


def _sync(server_id: int, user_id: int) -> None:
    server = servers.get(server_id)
    try:
        user_certs.sync(server, user_id)
    except IncusError as error:
        _report([f"{server['name']}: {error.message}"])


@bp.get("/users")
def users():
    rows = (
        get_db()
        .execute(
            "SELECT users.*, (SELECT COUNT(*) FROM grants WHERE user_id = users.id) AS grant_count "
            "FROM users ORDER BY username"
        )
        .fetchall()
    )
    return render_template("admin/users.html", rows=rows)


@bp.route("/users/new", methods=["GET", "POST"])
def user_create():
    form = request.form
    if request.method == "POST":
        username = form.get("username", "").strip()
        password = form.get("password", "")
        try:
            if not USERNAME.fullmatch(username):
                raise ValueError(USERNAME_RULE)
            check_password(password)
            if password != form.get("confirm", ""):
                raise ValueError("The passwords do not match.")
            db = get_db()
            if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise ValueError(f"A user named {username} already exists.")
            cursor = db.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), int(bool(form.get("is_admin")))),
            )
            db.commit()
        except ValueError as error:
            flash(str(error), "error")
        else:
            flash(f"User {username} created.", "success")
            return redirect(url_for("admin.user", user_id=cursor.lastrowid))
    return render_template("admin/user_new.html", form=form)


@bp.get("/users/<int:user_id>")
def user(user_id):
    return render_template(
        "admin/user.html",
        user=_user(user_id),
        grants=access.grants_for(user_id),
        servers=servers.all_servers(),
        project_names=_project_names(),
        roles=access.ROLE_LABELS,
        entries=audit.recent(20, _user(user_id)["username"]),
        certs={
            row["server_id"]: row
            for row in get_db().execute("SELECT * FROM user_certs WHERE user_id = ?", (user_id,))
        },
    )


@bp.post("/users/<int:user_id>/settings")
def user_settings(user_id):
    target = _user(user_id)
    make_admin = bool(request.form.get("is_admin"))
    disable = bool(request.form.get("disabled"))
    losing = target["is_admin"] and not target["disabled"] and (disable or not make_admin)
    if losing and not _other_active_admins(user_id):
        flash(
            "The console needs at least one active admin. Make someone else an admin first.",
            "error",
        )
        return redirect(url_for("admin.user", user_id=user_id))
    db = get_db()
    db.execute(
        "UPDATE users SET is_admin = ?, disabled = ? WHERE id = ?",
        (int(make_admin), int(disable), user_id),
    )
    db.commit()
    flash(f"User {target['username']} saved.", "success")
    _report(user_certs.sync_all(user_id))
    return redirect(url_for("admin.user", user_id=user_id))


@bp.post("/users/<int:user_id>/password")
def user_password(user_id):
    target = _user(user_id)
    password = request.form.get("password", "")
    try:
        check_password(password)
        if password != request.form.get("confirm", ""):
            raise ValueError("The passwords do not match.")
    except ValueError as error:
        flash(str(error), "error")
    else:
        db = get_db()
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
        db.commit()
        flash(f"Password for {target['username']} changed.", "success")
    return redirect(url_for("admin.user", user_id=user_id))


@bp.post("/users/<int:user_id>/delete")
def user_delete(user_id):
    target = _user(user_id)
    if target["id"] == g.user["id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin.user", user_id=user_id))
    if target["is_admin"] and not target["disabled"] and not _other_active_admins(user_id):
        flash("The console needs at least one active admin.", "error")
        return redirect(url_for("admin.user", user_id=user_id))
    problems = []
    for server in servers.all_servers():
        try:
            user_certs.remove(server, user_id)
        except IncusError as error:
            problems.append(f"{server['name']}: {error.message}")
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"User {target['username']} deleted.", "success")
    if problems:
        flash(
            "Could not remove the user's certificate from: "
            + "; ".join(problems)
            + ". Remove it there with 'incus config trust remove'.",
            "error",
        )
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>/grants")
def grant_add(user_id):
    target = _user(user_id)
    form = request.form
    project = form.get("project", "").strip() or access.ALL_PROJECTS
    role = form.get("role", "")
    try:
        server_id = int(form.get("server_id", ""))
    except ValueError:
        server_id = 0
    if servers.get(server_id) is None or role not in access.ROLES:
        flash("Pick a server and a role.", "error")
        return redirect(url_for("admin.user", user_id=user_id))
    db = get_db()
    db.execute(
        "INSERT INTO grants (user_id, server_id, project, role) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (user_id, server_id, project) DO UPDATE SET role = excluded.role",
        (user_id, server_id, project, role),
    )
    db.commit()
    where = "all projects" if project == access.ALL_PROJECTS else f"project {project}"
    flash(f"{target['username']} is now {access.ROLE_LABELS[role]} in {where}.", "success")
    _sync(server_id, user_id)
    return redirect(url_for("admin.user", user_id=user_id))


@bp.post("/users/<int:user_id>/grants/<int:grant_id>/delete")
def grant_delete(user_id, grant_id):
    db = get_db()
    row = db.execute(
        "SELECT server_id FROM grants WHERE id = ? AND user_id = ?", (grant_id, user_id)
    ).fetchone()
    db.execute("DELETE FROM grants WHERE id = ? AND user_id = ?", (grant_id, user_id))
    db.commit()
    flash("Access removed.", "success")
    if row:
        _sync(row["server_id"], user_id)
    return redirect(url_for("admin.user", user_id=user_id))


@bp.get("/audit")
def audit_log():
    username = request.args.get("user") or None
    return render_template("admin/audit.html", entries=audit.recent(500, username), user=username)


@account_bp.route("/account", methods=["GET", "POST"])
def account():
    user = _user(g.user["id"])
    if request.method == "POST":
        password = request.form.get("password", "")
        try:
            if not check_password_hash(user["password_hash"], request.form.get("current", "")):
                raise ValueError("Your current password is not right.")
            check_password(password)
            if password != request.form.get("confirm", ""):
                raise ValueError("The new passwords do not match.")
        except ValueError as error:
            flash(str(error), "error")
        else:
            db = get_db()
            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(password), user["id"]),
            )
            db.commit()
            flash("Password changed.", "success")
            return redirect(url_for("account.account"))
    return render_template(
        "account.html",
        user=user,
        grants=access.grants_for(user["id"]),
        roles=access.ROLE_LABELS,
    )
