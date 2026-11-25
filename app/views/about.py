"""About: version, licence and the details a bug report needs (like pgAdmin's)."""

from __future__ import annotations

import os
import platform
import sys
from importlib import metadata
from pathlib import Path

from flask import Blueprint, current_app, g, render_template, request

from .. import access, servers, user_certs
from ..certs import ensure_client_cert, fingerprint
from ..db import path_for
from ..incus import IncusError
from ..version import build

bp = Blueprint("about", __name__)

HOME = "https://github.com/openrds-io/incus-console"


def _package(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "-"


def _role() -> str:
    if access.is_admin():
        return "Console admin"
    return "User"


def _incus(sid: int | None) -> list[tuple[str, str]]:
    """The current Incus server, seen with the signed-in user's own access."""
    server = servers.get(sid) if sid else None
    if server is None or not access.visible_servers([server]):
        return []
    rows = [("Server", server["name"])]
    try:
        client = user_certs.client(server, g.user, "default") or servers.client_for(server)
        info = client.server_info()
    except IncusError as error:
        return [*rows, ("Status", error.message)]
    env = info.get("environment") or {}
    rows += [
        ("Incus version", env.get("server_version") or "-"),
        (
            "API",
            f"{info.get('api_version', '-')}, {len(info.get('api_extensions') or [])} extensions",
        ),
        ("Connection", "Local socket" if server["kind"] == "unix" else "HTTPS"),
    ]
    if server["kind"] == "https":
        per_user = user_certs.applies(server) and not access.is_admin()
        rows.append(
            (
                "Acting as",
                "Your own restricted certificate" if per_user else "The console's certificate",
            )
        )
    return rows


def _admin_details() -> list[tuple[str, str]]:
    config = current_app.config
    data_dir = config["DATA_DIR"]
    config_file = os.environ.get("INCUS_CONSOLE_CONFIG", "/etc/incus-console/config.py")
    crt, _key = ensure_client_cert(data_dir)
    software = os.environ.get("SERVER_SOFTWARE", "")
    web = f"gunicorn {_package('gunicorn')}" if "gunicorn" in software else "Flask's own server"
    rows = [
        (
            "Configuration file",
            config_file + ("" if Path(config_file).exists() else " (not present)"),
        ),
        ("Data folder", data_dir),
        ("Database", str(path_for(data_dir))),
        ("Console certificate", fingerprint(crt)),
        ("Python", sys.version.split()[0]),
        ("Flask", _package("flask")),
        ("Web server", web),
        ("Operating system", platform.platform()),
    ]
    if config["SERVER_MODE"]:
        on = "On" if config["PER_USER_CERTIFICATES"] else "Off"
        rows.insert(4, ("Per-user certificates", on))
    return rows


@bp.get("/about")
def about():
    try:
        sid = int(request.args.get("sid", ""))
    except ValueError:
        sid = None
    details = {
        "build": build(),
        "home": HOME,
        "mode": "Server" if current_app.config["SERVER_MODE"] else "Desktop",
        "user": g.user["username"] if current_app.config["SERVER_MODE"] else None,
        "role": _role(),
        "incus": _incus(sid),
        "admin": _admin_details() if access.is_admin() else [],
    }
    partial = request.headers.get("X-Partial") == "1"
    return render_template("about.html", partial=partial, **details)
