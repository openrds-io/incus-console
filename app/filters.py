"""Template filters."""

from __future__ import annotations


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


def init_app(app) -> None:
    app.add_template_filter(filesize)
    app.add_template_filter(when)
    app.add_template_filter(addresses)
