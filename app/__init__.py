"""Incus Console: a desktop and web console for Incus by your friends at OpenRDS.

Flask, server-rendered Jinja2, pytest.
`flask run` finds this package by its name.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from flask import Flask, g, render_template, request

from . import access, audit, auth, db, filters
from .incus import IncusConnectionError, IncusError
from .version import __version__, build

HOST = re.compile(r"[A-Za-z0-9.\-:\[\]]+")


def _secret_key(data_dir: Path) -> bytes:
    key_file = data_dir / "secret_key"
    if not key_file.exists():
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(32))
    return key_file.read_bytes()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object("app.config")
    config_file = os.environ.get("INCUS_CONSOLE_CONFIG", "/etc/incus-console/config.py")
    app.config.from_pyfile(config_file, silent=True)
    app.config.from_prefixed_env("INCUS_CONSOLE")
    if test_config:
        app.config.update(test_config)

    if app.config["BEHIND_PROXY"]:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        app.config["SESSION_COOKIE_SECURE"] = True

    data_dir = Path(app.config["DATA_DIR"]).expanduser()
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    app.config["DATA_DIR"] = str(data_dir)
    app.secret_key = _secret_key(data_dir)

    db.init_app(app)
    auth.init_app(app)
    access.init_app(app)
    audit.init_app(app)
    filters.init_app(app)

    from .views import (  # noqa: F401
        about,
        admin,
        cluster,
        cluster_upkeep,
        events,
        files,
        images,
        instances,
        networks,
        oci,
        operations,
        profiles,
        projects,
        scoped,
        storage,
        volumes,
    )
    from .views import servers as servers_view

    app.register_blueprint(servers_view.bp)
    app.register_blueprint(about.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(admin.account_bp)
    app.register_blueprint(scoped.bp)

    @app.url_defaults
    def _current_server(endpoint, values):
        # Links between pages of the same server do not need to repeat its id.
        if "sid" in values or "sid" not in g:
            return
        if app.url_map.is_endpoint_expecting(endpoint, "sid"):
            values["sid"] = g.sid

    @app.context_processor
    def _globals():
        return {
            "server_mode": app.config["SERVER_MODE"],
            "version": __version__,
            "build": build(),
        }

    @app.after_request
    def _security_headers(response):
        # Vanilla draws its icons as data: images; xterm.js styles itself at run
        # time; the terminal's websocket goes back to this host.
        policy = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
        if HOST.fullmatch(request.host):
            policy += f"; connect-src 'self' ws://{request.host} wss://{request.host}"
        response.headers.setdefault("Content-Security-Policy", policy)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.errorhandler(403)
    def _forbidden(_error):
        return render_template("forbidden.html"), 403

    @app.errorhandler(IncusError)
    def _incus_error(error: IncusError):
        # Tells the server pages not to ask the (possibly unreachable) server for more.
        g.incus_failed = True
        status = 502 if isinstance(error, IncusConnectionError) else 500
        if error.status in (403, 404):
            status = error.status
        return render_template("error.html", error=error, path=request.path), status

    return app
