"""Server mode: users and groups, their access, the audit log, and each user's own account."""

from __future__ import annotations

import csv
import io
import re

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .. import access, audit, servers, user_certs
from ..auth import check_password, end_sessions, forget_failures, is_locked
from ..db import get_db
from ..incus import IncusError

bp = Blueprint("admin", __name__, url_prefix="/admin")
account_bp = Blueprint("account", __name__)

USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
USERNAME_RULE = (
    "Use up to 64 letters, digits, dots, dashes, underscores or @, starting with a letter or digit."
)
GROUP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@-]{0,63}")
GROUP_NAME_RULE = (
    "Use up to 64 letters, digits, spaces, dots, dashes, underscores or @, "
    "starting with a letter or digit."
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


def _grant_form():
    """(server_id, project, role) from a "Give access" form, or None after flashing why."""
    form = request.form
    project = form.get("project", "").strip() or access.ALL_PROJECTS
    role = form.get("role", "")
    try:
        server_id = int(form.get("server_id", ""))
    except ValueError:
        server_id = 0
    if servers.get(server_id) is None or role not in access.ROLES:
        flash("Pick a server and a role.", "error")
        return None
    return server_id, project, role


def _where(project: str) -> str:
    return "all projects" if project == access.ALL_PROJECTS else f"project {project}"


@bp.get("/users")
def users():
    rows = (
        get_db()
        .execute(
            "SELECT users.*, "
            "(SELECT COUNT(*) FROM grants WHERE user_id = users.id) AS grant_count, "
            "(SELECT COUNT(*) FROM group_members WHERE user_id = users.id) AS group_count "
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
    target = _user(user_id)
    db = get_db()
    return render_template(
        "admin/user.html",
        user=target,
        grants=access.grants_for(user_id),
        group_grants=access.group_grants_for(user_id),
        groups=db.execute(
            "SELECT user_groups.* FROM user_groups JOIN group_members "
            "ON group_members.group_id = user_groups.id WHERE group_members.user_id = ? "
            "ORDER BY user_groups.name",
            (user_id,),
        ).fetchall(),
        other_groups=db.execute(
            "SELECT * FROM user_groups WHERE id NOT IN "
            "(SELECT group_id FROM group_members WHERE user_id = ?) ORDER BY name",
            (user_id,),
        ).fetchall(),
        servers=servers.all_servers(),
        project_names=_project_names(),
        roles=access.ROLE_LABELS,
        entries=audit.recent(20, target["username"]),
        locked=is_locked(target["username"]),
        certs={
            row["server_id"]: row
            for row in db.execute("SELECT * FROM user_certs WHERE user_id = ?", (user_id,))
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
        # Whoever knew the old password is out, and old failed attempts are forgiven.
        epoch = end_sessions(user_id)
        forget_failures(user_id)
        if user_id == g.user["id"]:
            session["epoch"] = epoch
        flash(f"Password for {target['username']} changed. Their sessions were ended.", "success")
    return redirect(url_for("admin.user", user_id=user_id))


@bp.post("/users/<int:user_id>/sign-out")
def user_sign_out(user_id):
    target = _user(user_id)
    end_sessions(user_id)
    flash(f"{target['username']} is signed out everywhere.", "success")
    if user_id == g.user["id"]:
        return redirect(url_for("auth.login"))
    return redirect(url_for("admin.user", user_id=user_id))


@bp.post("/users/<int:user_id>/unlock")
def user_unlock(user_id):
    target = _user(user_id)
    forget_failures(user_id)
    flash(f"{target['username']} can sign in again.", "success")
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
    parsed = _grant_form()
    if parsed is None:
        return redirect(url_for("admin.user", user_id=user_id))
    server_id, project, role = parsed
    db = get_db()
    db.execute(
        "INSERT INTO grants (user_id, server_id, project, role) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (user_id, server_id, project) DO UPDATE SET role = excluded.role",
        (user_id, server_id, project, role),
    )
    db.commit()
    label = access.ROLE_LABELS[role]
    flash(f"{target['username']} is now {label} in {_where(project)}.", "success")
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


# -- groups ----------------------------------------------------------------


def _group(group_id: int):
    row = get_db().execute("SELECT * FROM user_groups WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _group_members(group_id: int) -> list[int]:
    rows = get_db().execute("SELECT user_id FROM group_members WHERE group_id = ?", (group_id,))
    return [row[0] for row in rows]


def _group_servers(group_id: int) -> list[int]:
    rows = get_db().execute(
        "SELECT DISTINCT server_id FROM group_grants WHERE group_id = ?", (group_id,)
    )
    return [row[0] for row in rows]


def _group_name_error(name: str, group_id: int | None = None) -> str | None:
    if not GROUP_NAME.fullmatch(name):
        return GROUP_NAME_RULE
    clash = (
        get_db()
        .execute("SELECT 1 FROM user_groups WHERE name = ? AND id IS NOT ?", (name, group_id))
        .fetchone()
    )
    return f"A group named {name} already exists." if clash else None


def _back(group_id: int):
    """Membership changes can come from the group page or a user's page."""
    user_id = request.form.get("back_user", type=int)
    if user_id:
        return redirect(url_for("admin.user", user_id=user_id))
    return redirect(url_for("admin.group", group_id=group_id))


@bp.get("/groups")
def groups():
    rows = (
        get_db()
        .execute(
            "SELECT user_groups.*, "
            "(SELECT COUNT(*) FROM group_members WHERE group_id = user_groups.id) "
            "AS member_count, "
            "(SELECT COUNT(*) FROM group_grants WHERE group_id = user_groups.id) AS grant_count "
            "FROM user_groups ORDER BY name"
        )
        .fetchall()
    )
    return render_template("admin/groups.html", rows=rows)


@bp.route("/groups/new", methods=["GET", "POST"])
def group_create():
    form = request.form
    if request.method == "POST":
        name = form.get("name", "").strip()
        problem = _group_name_error(name)
        if problem:
            flash(problem, "error")
        else:
            db = get_db()
            cursor = db.execute(
                "INSERT INTO user_groups (name, description) VALUES (?, ?)",
                (name, form.get("description", "").strip()),
            )
            db.commit()
            flash(f"Group {name} created.", "success")
            return redirect(url_for("admin.group", group_id=cursor.lastrowid))
    return render_template("admin/group_new.html", form=form)


@bp.get("/groups/<int:group_id>")
def group(group_id):
    db = get_db()
    return render_template(
        "admin/group.html",
        group=_group(group_id),
        members=db.execute(
            "SELECT users.* FROM users JOIN group_members ON group_members.user_id = users.id "
            "WHERE group_members.group_id = ? ORDER BY username",
            (group_id,),
        ).fetchall(),
        non_members=db.execute(
            "SELECT * FROM users WHERE id NOT IN "
            "(SELECT user_id FROM group_members WHERE group_id = ?) ORDER BY username",
            (group_id,),
        ).fetchall(),
        grants=db.execute(
            "SELECT group_grants.*, servers.name AS server_name FROM group_grants "
            "JOIN servers ON servers.id = group_grants.server_id WHERE group_id = ? "
            "ORDER BY servers.name, project",
            (group_id,),
        ).fetchall(),
        servers=servers.all_servers(),
        project_names=_project_names(),
        roles=access.ROLE_LABELS,
    )


@bp.post("/groups/<int:group_id>/settings")
def group_settings(group_id):
    _group(group_id)
    name = request.form.get("name", "").strip()
    problem = _group_name_error(name, group_id)
    if problem:
        flash(problem, "error")
    else:
        db = get_db()
        db.execute(
            "UPDATE user_groups SET name = ?, description = ? WHERE id = ?",
            (name, request.form.get("description", "").strip(), group_id),
        )
        db.commit()
        flash(f"Group {name} saved.", "success")
    return redirect(url_for("admin.group", group_id=group_id))


@bp.post("/groups/<int:group_id>/delete")
def group_delete(group_id):
    target = _group(group_id)
    members, server_ids = _group_members(group_id), _group_servers(group_id)
    db = get_db()
    db.execute("DELETE FROM user_groups WHERE id = ?", (group_id,))
    db.commit()
    flash(f"Group {target['name']} deleted. Its members keep their own access.", "success")
    _report(user_certs.sync_many(members, server_ids))
    return redirect(url_for("admin.groups"))


@bp.post("/groups/<int:group_id>/members")
def group_member_add(group_id):
    target = _group(group_id)
    member = _user(request.form.get("user_id", type=int) or 0)
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
        (group_id, member["id"]),
    )
    db.commit()
    flash(f"{member['username']} added to {target['name']}.", "success")
    _report(user_certs.sync_many([member["id"]], _group_servers(group_id)))
    return _back(group_id)


@bp.post("/users/<int:user_id>/groups")
def user_group_add(user_id):
    member = _user(user_id)
    target = _group(request.form.get("group_id", type=int) or 0)
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
        (target["id"], user_id),
    )
    db.commit()
    flash(f"{member['username']} added to {target['name']}.", "success")
    _report(user_certs.sync_many([user_id], _group_servers(target["id"])))
    return redirect(url_for("admin.user", user_id=user_id))


@bp.post("/groups/<int:group_id>/members/<int:user_id>/delete")
def group_member_delete(group_id, user_id):
    target, member = _group(group_id), _user(user_id)
    db = get_db()
    db.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id))
    db.commit()
    flash(f"{member['username']} removed from {target['name']}.", "success")
    _report(user_certs.sync_many([user_id], _group_servers(group_id)))
    return _back(group_id)


@bp.post("/groups/<int:group_id>/grants")
def group_grant_add(group_id):
    target = _group(group_id)
    parsed = _grant_form()
    if parsed is None:
        return redirect(url_for("admin.group", group_id=group_id))
    server_id, project, role = parsed
    db = get_db()
    db.execute(
        "INSERT INTO group_grants (group_id, server_id, project, role) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (group_id, server_id, project) DO UPDATE SET role = excluded.role",
        (group_id, server_id, project, role),
    )
    db.commit()
    flash(
        f"Members of {target['name']} are now {access.ROLE_LABELS[role]} in {_where(project)}.",
        "success",
    )
    _report(user_certs.sync_many(_group_members(group_id), [server_id]))
    return redirect(url_for("admin.group", group_id=group_id))


@bp.post("/groups/<int:group_id>/grants/<int:grant_id>/delete")
def group_grant_delete(group_id, grant_id):
    db = get_db()
    row = db.execute(
        "SELECT server_id FROM group_grants WHERE id = ? AND group_id = ?", (grant_id, group_id)
    ).fetchone()
    db.execute("DELETE FROM group_grants WHERE id = ? AND group_id = ?", (grant_id, group_id))
    db.commit()
    flash("Access removed from the group.", "success")
    if row:
        _report(user_certs.sync_many(_group_members(group_id), [row["server_id"]]))
    return redirect(url_for("admin.group", group_id=group_id))


DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
AUDIT_PAGE = 500
AUDIT_EXPORT = 100_000


def _audit_filters() -> dict:
    """The audit log's filters from the address; anything malformed is dropped."""
    args = request.args
    server = args.get("server", "")
    return {
        "username": args.get("user", "").strip() or None,
        "server_id": int(server) if server.isdecimal() else None,
        "action": args.get("action", "").strip() or None,
        "since": args.get("since") if DAY.fullmatch(args.get("since", "")) else None,
        "until": args.get("until") if DAY.fullmatch(args.get("until", "")) else None,
    }


@bp.get("/audit")
def audit_log():
    filters = _audit_filters()
    entries = audit.recent(AUDIT_PAGE, **filters)
    return render_template(
        "admin/audit.html",
        entries=entries,
        user=filters["username"],
        filters=filters,
        filtered=any(filters.values()),
        servers=servers.all_servers(),
        full=len(entries) >= AUDIT_PAGE,
        page=AUDIT_PAGE,
        query=request.query_string.decode(),
    )


def _csv_cell(value) -> str:
    """A cell that a spreadsheet will not run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@bp.get("/audit.csv")
def audit_csv():
    """The audit log, with the same filters as the page, as a CSV file."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["when_utc", "user", "server", "project", "action", "path", "status", "outcome"]
    )
    for e in audit.recent(AUDIT_EXPORT, **_audit_filters()):
        row = (e["at"], e["username"], e["server_name"], e["project"], e["action"], e["path"])
        writer.writerow([_csv_cell(cell) for cell in (*row, e["status"], e["outcome"])])
    response = make_response(out.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = 'attachment; filename="incus-console-audit.csv"'
    return response


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
            # Every other session of theirs ends; this one carries on.
            session["epoch"] = end_sessions(user["id"])
            flash("Password changed. You are signed out everywhere else.", "success")
            return redirect(url_for("account.account"))
    return render_template(
        "account.html",
        user=user,
        grants=access.effective_grants(user["id"]),
        roles=access.ROLE_LABELS,
    )
