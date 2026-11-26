"""Application containers (OCI): registries, published ports, volumes, new images."""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit, urlunsplit

from flask import current_app, flash, g, redirect, render_template, request, url_for

from ..filters import instance_kind
from ..incus import IncusError
from .scoped import DONE, RUNNING, bp, start

OCI_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[a-f0-9]{64})?")
PORT = re.compile(r"(?:(\d{1,5}):)?(\d{1,5})(?:/(tcp|udp))?")
VOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")


def oci_source(reference: str, default_registry: str) -> tuple[str, str]:
    """(registry URL, image) from ``nginx:alpine`` or ``ghcr.io/owner/app:tag``."""
    reference = reference.strip().removeprefix("docker://").removeprefix("https://")
    first, slash, rest = reference.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        return f"https://{first}", rest
    return default_registry.rstrip("/"), reference


def with_credentials(server: str, username: str, password: str) -> str:
    """The registry URL with a login in it, which is how Incus takes one."""
    username = username.strip()
    if not username and not password:
        return server
    if not username or not password:
        raise IncusError(400, "A private registry needs both a username and a password or token.")
    parts = urlsplit(server)
    login = f"{quote(username, safe='')}:{quote(password, safe='')}"
    return urlunsplit(parts._replace(netloc=f"{login}@{parts.hostname}" + _port(parts)))


def _port(parts) -> str:
    return f":{parts.port}" if parts.port else ""


def without_credentials(server: str | None) -> str:
    """A registry URL that is safe to show: Incus keeps the login in it."""
    if not server or "@" not in server:
        return server or ""
    parts = urlsplit(server)
    return urlunsplit(parts._replace(netloc=(parts.hostname or "") + _port(parts)))


def reference(server: str | None, alias: str | None, default_registry: str) -> str:
    """What someone would type for this image: the reverse of oci_source()."""
    if not alias:
        return ""
    shown = without_credentials(server).rstrip("/")
    if not shown or shown == default_registry.rstrip("/"):
        return alias
    return f"{shown.removeprefix('https://')}/{alias}"


def source(reference_text: str, username: str = "", password: str = "") -> dict:
    """The Incus image source for what the form holds. Raises IncusError."""
    server, alias = oci_source(reference_text, current_app.config["OCI_REGISTRY"])
    if not alias:
        raise IncusError(400, "Choose an image.")
    if not OCI_IMAGE.fullmatch(alias):
        raise IncusError(400, f"{alias!r} is not an image name, such as nginx:alpine.")
    return {
        "type": "image",
        "mode": "pull",
        "protocol": "oci",
        "server": with_credentials(server, username, password),
        "alias": alias,
    }


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def port_devices(text: str) -> dict:
    """Proxy devices from lines such as ``8080:80`` (host port, then the container's).

    One port alone means the same on both sides; ``/udp`` after it for UDP.
    """
    devices = {}
    for line in _lines(text):
        found = PORT.fullmatch(line.replace(" ", ""))
        if not found:
            raise IncusError(400, f"{line!r} is not a port such as 8080:80.")
        inside = int(found.group(2))
        outside = int(found.group(1) or inside)
        protocol = found.group(3) or "tcp"
        if not (0 < inside < 65536 and 0 < outside < 65536):
            raise IncusError(400, f"{line!r}: ports go from 1 to 65535.")
        name = f"port-{outside}" + ("" if protocol == "tcp" else f"-{protocol}")
        if name in devices:
            raise IncusError(400, f"Port {outside} is listed twice.")
        devices[name] = {
            "type": "proxy",
            "listen": f"{protocol}:0.0.0.0:{outside}",
            "connect": f"{protocol}:127.0.0.1:{inside}",
        }
    return devices


def volume_mounts(text: str) -> dict[str, str]:
    """``{volume: path}`` from lines such as ``pgdata:/var/lib/postgresql``."""
    mounts = {}
    for line in _lines(text):
        name, colon, where = line.partition(":")
        name, where = name.strip(), where.strip()
        if not colon or not VOLUME_NAME.fullmatch(name) or not where.startswith("/"):
            raise IncusError(400, f"{line!r} is not a volume such as data:/var/lib/app.")
        if name in mounts:
            raise IncusError(400, f"Volume {name} is listed twice.")
        mounts[name] = where
    return mounts


def volume_devices(mounts: dict[str, str], pool: str, target: str | None, clustered: bool) -> dict:
    """Disk devices for the mounts, creating the volumes that do not exist yet.

    On a cluster a new volume lives on one member (unless the pool is shared),
    so it is made on the member the instance is going to.
    """
    if not mounts:
        return {}
    existing = {v["name"] for v in g.incus.volumes(pool) if v.get("type") == "custom"}
    missing = [name for name in mounts if name not in existing]
    if missing and clustered and (not target or target.startswith("@")):
        raise IncusError(
            400,
            "Choose the cluster member: new volumes are made on the member the container runs on.",
        )
    for name in missing:
        payload = {"name": name, "content_type": "filesystem", "config": {}}
        g.incus.wait(g.incus.create_volume(pool, payload, target=target))
    return {
        name: {"type": "disk", "pool": pool, "source": name, "path": where}
        for name, where in mounts.items()
    }


def root_pool(profile_names: list[str]) -> str:
    """The pool the chosen profiles put the root disk on (the last one wins)."""
    pool = "default"
    by_name = {p["name"]: p for p in g.incus.profiles()}
    for name in profile_names:
        root = ((by_name.get(name) or {}).get("devices") or {}).get("root") or {}
        if root.get("type") == "disk" and root.get("pool"):
            pool = root["pool"]
    return pool


def presets() -> dict:
    """OCI_PRESETS with each value as the text the form's boxes hold."""
    return {
        image: {key: "\n".join(lines) for key, lines in preset.items()}
        for image, preset in current_app.config["OCI_PRESETS"].items()
    }


def _current_image(instance: dict) -> str:
    """The image an application container was made from, as someone would type it."""
    fingerprint = (instance.get("config") or {}).get("volatile.base_image")
    if not fingerprint:
        return ""
    try:
        origin = g.incus.image(fingerprint).get("update_source") or {}
    except IncusError as error:
        if error.status == 0:
            raise
        return ""  # the image is no longer on the server
    return reference(origin.get("server"), origin.get("alias"), current_app.config["OCI_REGISTRY"])


@bp.route("/instances/<name>/update-image", methods=["GET", "POST"])
def instance_update_image(name):
    """Rebuild an application container on another (usually newer) image."""
    instance = g.incus.instance(name)
    if instance_kind(instance) != "oci":
        flash("Only application containers are updated by replacing their image.", "error")
        return redirect(url_for("srv.instance", name=name))
    form = request.form
    if request.method == "POST":
        try:
            new = source(
                form.get("oci_image", ""),
                form.get("registry_user", ""),
                form.get("registry_password", ""),
            )
        except IncusError as error:
            flash(error.message, "error")
        else:
            outcome = start(
                lambda: g.incus.rebuild_instance(name, new), f"{name} rebuilt on {new['alias']}."
            )
            if outcome == DONE:
                return redirect(url_for("srv.instance", name=name))
            if outcome == RUNNING:
                return redirect(url_for("srv.operations"))
    return render_template(
        "instances/update_image.html",
        instance=instance,
        image=form.get("oci_image") or _current_image(instance),
        form=form,
    )
