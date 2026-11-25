"""The image server's catalog, for the create form's image picker.

Image servers such as images.linuxcontainers.org publish a simplestreams index
at ``/streams/v1/images.json``. It is fetched by this app (never by the
browser), only from the configured DEFAULT_IMAGE_SERVER, and kept for an hour.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request

INDEX = "/streams/v1/images.json"
MAX_BYTES = 64 * 1024 * 1024
TTL_SECONDS = 3600

# Incus names CPUs the kernel's way; image servers use Debian's names.
ARCHITECTURES = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "armv7l": "armhf",
    "i686": "i386",
    "ppc64le": "ppc64el",
    "s390x": "s390x",
    "riscv64": "riscv64",
}
CONTAINER_FILES = {"squashfs", "root.tar.xz"}
VM_FILES = {"disk-kvm.img"}
FILES = {"container": CONTAINER_FILES, "virtual-machine": VM_FILES}

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def fetch(server: str, timeout: float = 20) -> dict:
    """The server's simplestreams image index."""
    if not server.startswith("https://"):
        raise ValueError("The image server must be an https:// address.")
    url = server.rstrip("/") + INDEX
    request = urllib.request.Request(url, headers={"User-Agent": "incus-console"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("The image index is too large.")
    return json.loads(body)


def _index(server: str) -> dict:
    with _lock:
        cached = _cache.get(server)
        if cached and time.monotonic() - cached[0] < TTL_SECONDS:
            return cached[1]
    index = fetch(server)
    with _lock:
        _cache[server] = (time.monotonic(), index)
    return index


def clear() -> None:
    with _lock:
        _cache.clear()


def _natural(value: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value or "")]


def _has_files(product: dict, files: set[str]) -> bool:
    for version in (product.get("versions") or {}).values():
        types = {item.get("ftype") for item in (version.get("items") or {}).values()}
        if types & files:
            return True
    return False


def container_images(index: dict, architectures: list[str], kind: str = "container") -> list[dict]:
    """Images of one kind (container or virtual-machine) for these Incus
    architectures, grouped by OS.

    Returns ``[{"os": "Debian", "images": [{"alias", "label", "variant"}, ...]}, ...]``
    with releases newest first.
    """
    wanted = {ARCHITECTURES.get(a, a) for a in architectures}
    by_os: dict[str, list[dict]] = {}
    for product in (index.get("products") or {}).values():
        if product.get("arch") not in wanted or not _has_files(product, FILES[kind]):
            continue
        aliases = [a.strip() for a in (product.get("aliases") or "").split(",") if a.strip()]
        if not aliases:
            continue
        os_name = product.get("os") or "Other"
        release = product.get("release_title") or product.get("release") or ""
        variant = product.get("variant") or "default"
        label = f"{os_name} {release}".strip()
        if variant != "default":
            label += f" ({variant})"
        by_os.setdefault(os_name, []).append(
            {
                "alias": min(aliases, key=len),
                "label": label,
                "variant": variant,
                "sort": (_natural(release), variant != "default", variant),
            }
        )
    groups = []
    for os_name in sorted(by_os, key=str.lower):
        images = sorted(by_os[os_name], key=lambda i: i["sort"])
        # Newest release first, the default variant before the others.
        images.sort(key=lambda i: i["sort"][0], reverse=True)
        groups.append(
            {"os": os_name, "images": [{k: v for k, v in i.items() if k != "sort"} for i in images]}
        )
    return groups


def for_server(server: str, architectures: list[str], kind: str = "container") -> list[dict]:
    return container_images(_index(server), architectures, kind)
