"""Template filters."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def filesize(value) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "-"
    if size <= 0:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "-"


def when(value) -> str:
    """Trim an RFC 3339 timestamp to ``YYYY-MM-DD HH:MM``; hide Go's zero time."""
    if not value or str(value).startswith("0001-"):
        return "-"
    return str(value)[:16].replace("T", " ")


def addresses(instance: dict, family: str = "inet") -> list[str]:
    """Global addresses of an instance, skipping loopback."""
    network = (instance.get("state") or {}).get("network") or {}
    found = []
    for name, nic in network.items():
        if name == "lo":
            continue
        for address in nic.get("addresses") or []:
            if address.get("family") == family and address.get("scope") == "global":
                found.append(address["address"])
    return found


KIND_LABELS = {
    "container": "System container",
    "oci": "Application container (OCI)",
}


def instance_kind(instance: dict) -> str:
    """container, or oci (an application container from an OCI image)."""
    config = instance.get("config") or {}
    if config.get("volatile.container.oci") == "true" or config.get("image.type") == "oci":
        return "oci"
    return "container"


def environment_names(config: dict) -> list[str]:
    """Names of the environment variables an instance config sets."""
    prefix = "environment."
    return sorted(key[len(prefix) :] for key in (config or {}) if key.startswith(prefix))


def no_credentials(server) -> str:
    """A registry or image server URL without the login Incus keeps in it."""
    if not server or "@" not in str(server):
        return server or ""
    parts = urlsplit(str(server))
    host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=host))


def init_app(app) -> None:
    app.add_template_filter(filesize)
    app.add_template_filter(when)
    app.add_template_filter(addresses)
    app.add_template_filter(instance_kind)
    app.add_template_filter(environment_names)
    app.add_template_filter(no_credentials)
    app.jinja_env.globals["kind_labels"] = KIND_LABELS
