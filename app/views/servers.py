from __future__ import annotations

from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import access, servers, user_certs
from ..certs import ensure_client_cert, fingerprint
from ..incus import IncusClient, IncusError, enroll_with_token
from ..incus.client import normalize_fingerprint

bp = Blueprint("servers", __name__)

UNTRUSTED = (
    "The server answered but does not trust this console yet. On the server run "
    "'incus config trust add incus-console' and add it here with the token instead."
)


@bp.before_request
def _admins_change_servers():
    if request.method == "POST" and not access.is_admin():
        abort(403)


@bp.get("/")
def home():
    rows = access.visible_servers(servers.all_servers())
    if len(rows) == 1:
        return redirect(url_for("srv.instances", sid=rows[0]["id"]))
    return redirect(url_for("servers.index"))


@bp.get("/servers")
def index():
    cert, _key = ensure_client_cert(current_app.config["DATA_DIR"])
    return render_template(
        "servers/index.html",
        servers=access.visible_servers(servers.all_servers()),
        detected=servers.detected_sockets() if access.is_admin() else [],
        client_fingerprint=fingerprint(cert),
    )


def _check_trusted(client: IncusClient) -> None:
    if client.server_info().get("auth") != "trusted":
        raise IncusError(403, UNTRUSTED)


@bp.post("/servers")
def add():
    form = request.form
    kind = form.get("kind", "")
    name = form.get("name", "").strip()
    cert, key = ensure_client_cert(current_app.config["DATA_DIR"])
    timeout = current_app.config["INCUS_TIMEOUT"]
    try:
        if kind == "unix":
            address = str(Path(form.get("address", "").strip()).expanduser())
            _check_trusted(IncusClient(socket_path=address, timeout=timeout))
            server_id = servers.add(name or "Local", "unix", address)
        elif kind == "token":
            result = enroll_with_token(form.get("token", ""), cert, key, timeout=timeout)
            server_id = servers.add(
                name or result["url"].removeprefix("https://"),
                "https",
                result["url"],
                result["fingerprint"],
            )
        elif kind == "https":
            address = form.get("address", "").strip()
            pin = normalize_fingerprint(form.get("fingerprint"))
            _check_trusted(
                IncusClient(url=address, cert=cert, key=key, fingerprint=pin, timeout=timeout)
            )
            server_id = servers.add(name or address.removeprefix("https://"), "https", address, pin)
        else:
            raise IncusError(400, "Unknown server type.")
    except (IncusError, ValueError) as error:
        flash(str(error), "error")
        return redirect(url_for("servers.index"))
    flash("Server added.", "success")
    return redirect(url_for("srv.instances", sid=server_id))


@bp.post("/servers/<int:server_id>/delete")
def delete(server_id):
    server = servers.get(server_id)
    if server is not None:
        user_certs.forget_server(server)
    servers.delete(server_id)
    flash("Server removed from this console. Nothing was changed on the server.", "success")
    return redirect(url_for("servers.index"))
