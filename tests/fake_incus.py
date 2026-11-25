"""An in-memory stand-in for the Incus API, for tests and for trying the UI.

Run a demo server with:  python -m tests.fake_incus /tmp/fake-incus.sock
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import http.server
import json
import os
import re
import secrets
import socketserver
import ssl
import struct
import sys
import threading
import uuid
from urllib.parse import parse_qs, unquote, urlsplit


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeIncus:
    LOGS = {"lxc.log": "lxc 20261125 INFO start\n", "lxc.conf": "lxc.uts.name = x\n"}

    def __init__(self):
        self.lock = threading.RLock()
        self.instances: dict[str, dict] = {}
        self.operations: dict[str, dict] = {}
        self.pools = {
            "default": {
                "name": "default",
                "driver": "zfs",
                "status": "Created",
                "description": "",
                "used_by": [],
            }
        }
        self.buckets: dict[str, dict[str, dict]] = {"default": {}}
        self.tokens: set[str] = set()
        self.trusted: list[str] = []  # fingerprints enrolled with a token
        self.sessions: dict[str, dict] = {}  # terminal sessions by operation id
        self.calls: list[tuple[str, str]] = []
        self.images: dict[str, dict] = {}
        self.op_done: dict[str, threading.Event] = {}
        self.subscribers: list = []  # /1.0/events websockets
        self.projects = {
            "default": {"name": "default", "description": "Default Incus project", "config": {}}
        }
        self.project_used_by: dict[str, list] = {}  # extra used_by entries for tests
        self.fs: dict[str, dict] = {}  # instance files, by instance
        self.writes: list = []  # (path, headers) of file writes
        self.volumes: dict[str, dict[str, dict]] = {}
        self.ended: dict[str, dict] = {}  # how finished operations ended
        self.forwards: dict[str, dict] = {"incusbr0": {}}
        self.networks = {
            "incusbr0": {
                "name": "incusbr0",
                "type": "bridge",
                "managed": True,
                "description": "",
                "config": {"ipv4.address": "10.0.3.1/24", "ipv4.nat": "true"},
                "status": "Created",
            },
            "eth0": {
                "name": "eth0",
                "type": "physical",
                "managed": False,
                "description": "",
                "config": {},
                "status": "",
            },
        }
        self.created: list[dict] = []  # bodies of instance creations from images
        self.published: list[dict] = []
        self.profiles = {
            "default": {
                "name": "default",
                "description": "Default Incus profile",
                "config": {},
                "devices": {
                    "eth0": {"type": "nic", "network": "incusbr0", "name": "eth0"},
                    "root": {"type": "disk", "path": "/", "pool": "default"},
                },
            },
            "big": {
                "name": "big",
                "description": "",
                "config": {"limits.cpu": "4"},
                "devices": {},
            },
        }
        self.puts: list[dict] = []  # bodies of instance, bucket and key PUTs
        self.no_bucket_drivers: set[str] = set()
        self.pending_ops = False  # True: operations stay Running

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def sync(metadata=None, status=200):
        body = {"type": "sync", "status": "Success", "status_code": 200, "metadata": metadata}
        return status, body

    @staticmethod
    def error(code, message):
        return code, {"type": "error", "error_code": code, "error": message}

    def start_op(self, description, err=""):
        op_id = str(uuid.uuid4())
        running = self.pending_ops and not err
        op = {
            "id": op_id,
            "class": "task",
            "description": description,
            "created_at": _now(),
            "status": "Failure" if err else ("Running" if running else "Success"),
            "status_code": 400 if err else (103 if running else 200),
            "may_cancel": running,
            "err": err,
        }
        self.operations[op_id] = op
        self.op_done[op_id] = threading.Event()
        if not running:
            self.op_done[op_id].set()
        body = {
            "type": "async",
            "status": "Operation created",
            "status_code": 100,
            "operation": f"/1.0/operations/{op_id}",
            "metadata": op,
        }
        self.emit("operation", **op)
        return 202, body

    def _volume(self, pool, volume):
        used_by = [
            f"/1.0/instances/{i['name']}"
            for i in self.instances.values()
            if any(
                d.get("type") == "disk" and d.get("pool") == pool and d.get("source") == volume
                for d in (i.get("devices") or {}).values()
            )
        ]
        return {**self.volumes[pool][volume], "used_by": used_by}

    def _volumes(self, method, pool, rest, body):
        volumes = self.volumes.setdefault(pool, {})
        match (method, rest):
            case ("GET", []):
                custom = [self._volume(pool, v) for v in volumes]
                own = [
                    {"name": i, "type": "container", "config": {}, "used_by": []}
                    for i in self.instances
                ]
                return self.sync(custom + own)
            case ("POST", ["custom"]):
                name = body.get("name", "")
                if name in volumes:
                    return self.error(409, "Volume by that name already exists")
                volumes[name] = {
                    "name": name,
                    "type": "custom",
                    "description": body.get("description", ""),
                    "content_type": body.get("content_type", "filesystem"),
                    "config": body.get("config") or {},
                    "created_at": _now(),
                    "snapshots": [],
                }
                return self.sync()
            case (_, ["custom", name, *more]):
                if name not in volumes:
                    return self.error(404, "Storage volume not found")
                volume = volumes[name]
                match (method, more):
                    case ("GET", []):
                        return self.sync(self._volume(pool, name))
                    case ("PUT", []):
                        if "restore" in body:
                            if body["restore"] not in [s["name"] for s in volume["snapshots"]]:
                                return self.error(404, "Snapshot not found")
                            return self.sync()
                        size = (body.get("config") or {}).get("size", "")
                        if size.startswith("1MiB"):
                            return self.error(400, "Volume would be smaller than its contents")
                        volume["description"] = body.get("description", "")
                        volume["config"] = body.get("config") or {}
                        return self.sync()
                    case ("POST", []):
                        new_name = body.get("name", "")
                        if self._volume(pool, name)["used_by"]:
                            return self.error(400, "Volume is still in use")
                        volumes[new_name] = {**volumes.pop(name), "name": new_name}
                        return self.sync()
                    case ("DELETE", []):
                        if self._volume(pool, name)["used_by"]:
                            return self.error(400, "The storage volume is still in use")
                        del volumes[name]
                        return self.sync()
                    case ("GET", ["snapshots"]):
                        return self.sync(
                            [{**s, "name": f"{name}/{s['name']}"} for s in volume["snapshots"]]
                        )
                    case ("POST", ["snapshots"]):
                        volume["snapshots"].append(
                            {
                                "name": body.get("name", ""),
                                "created_at": _now(),
                                "expires_at": "0001-01-01T00:00:00Z",
                            }
                        )
                        return self.start_op(f"Snapshotting volume {name}")
                    case ("DELETE", ["snapshots", snapshot]):
                        before = len(volume["snapshots"])
                        volume["snapshots"] = [
                            s for s in volume["snapshots"] if s["name"] != snapshot
                        ]
                        if len(volume["snapshots"]) == before:
                            return self.error(404, "Snapshot not found")
                        return self.start_op(f"Deleting snapshot {snapshot}")
        return self.error(404, "not found")

    def add_volume(self, pool, name, **fields):
        self._volumes("POST", pool, ["custom"], {"name": name, **fields})

    def add_pool(self, name, driver="dir"):
        self.pools[name] = {
            "name": name,
            "driver": driver,
            "status": "Created",
            "description": "",
            "used_by": [],
        }

    def add_bucket(self, pool, name, **body):
        return self._buckets("POST", pool, [], {"name": name, **body})

    def add_instance(self, name, status="Running", kind="container", image="Debian 13"):
        running = status == "Running"
        self.instances[name] = {
            "name": name,
            "type": kind,
            "status": status,
            "status_code": 103 if running else 102,
            "architecture": "aarch64",
            "description": "",
            "created_at": _now(),
            "last_used_at": _now(),
            "ephemeral": False,
            "stateful": False,
            "profiles": ["default"],
            "config": {"image.description": image, "volatile.eth0.hwaddr": "10:66:6a:00:00:01"},
            "devices": {},
            "expanded_config": {"image.description": image, "limits.cpu": "2"},
            "expanded_devices": {
                "eth0": {"type": "nic", "network": "incusbr0", "name": "eth0"},
                "root": {"type": "disk", "path": "/", "pool": "default"},
            },
            "snapshots": [],
        }

    def _full(self, instance):
        running = instance["status"] == "Running"
        network = None
        if running:
            index = sorted(self.instances).index(instance["name"]) + 10
            network = {
                "lo": {"addresses": [{"family": "inet", "address": "127.0.0.1", "scope": "local"}]},
                "eth0": {
                    "addresses": [
                        {"family": "inet", "address": f"10.0.3.{index}", "scope": "global"},
                        {"family": "inet6", "address": f"fd42::{index}", "scope": "global"},
                    ]
                },
            }
        state = {
            "status": instance["status"],
            "processes": 12 if running else -1,
            "memory": {"usage": 64 * 1024 * 1024 if running else 0},
            "network": network,
        }
        # Like Incus: the expanded devices are the profiles' with the instance's own on top.
        expanded = {**instance["expanded_devices"], **(instance.get("devices") or {})}
        return {**instance, "expanded_devices": expanded, "state": state}

    # -- routing -----------------------------------------------------------

    def handle(self, method, raw_path, body, peer_cert=None):
        parts = urlsplit(raw_path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        segments = [unquote(s) for s in parts.path.strip("/").split("/")]
        if method == "GET" and len(segments) == 4 and segments[1:4:2] == ["operations", "wait"]:
            return self._wait(segments[2], float(query.get("timeout", 30)))
        with self.lock:
            self.calls.append((method, parts.path))
            if segments[0] != "1.0":
                return self.error(404, "not found")
            return self._route(method, segments[1:], query, body or {}, peer_cert)

    def _route(self, method, seg, query, body, peer_cert):
        match (method, seg):
            case ("GET", []):
                return self.sync(
                    {
                        "api_version": "1.0",
                        "auth": "trusted",
                        "config": {"core.storage_buckets_address": ":8555"},
                        "environment": {
                            "server_name": "fake",
                            "server_version": "6.0",
                            "architectures": ["aarch64"],
                        },
                    }
                )
            case ("POST", ["certificates"]):
                if body.get("trust_token") not in self.tokens:
                    return self.error(403, "Invalid trust token")
                if not peer_cert:
                    return self.error(400, "No client certificate provided")
                self.trusted.append(hashlib.sha256(peer_cert).hexdigest())
                return self.sync()
            case ("GET", ["projects"]):
                return self.sync([self._project(n) for n in self.projects])
            case ("POST", ["projects"]):
                name = body.get("name", "")
                if name in self.projects:
                    return self.error(409, "Project already exists")
                self.projects[name] = {
                    "name": name,
                    "description": body.get("description", ""),
                    "config": body.get("config") or {},
                }
                return self.sync()
            case (_, ["projects", name, *rest]):
                if name not in self.projects:
                    return self.error(404, "Project not found")
                return self._project_op(method, name, rest, body)
            case ("GET", ["profiles"]):
                return self.sync([self._profile(name) for name in self.profiles])
            case ("POST", ["profiles"]):
                name = body.get("name", "")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                    return self.error(400, f'Invalid profile name "{name}"')
                if name in self.profiles:
                    return self.error(409, "Profile already exists")
                self.profiles[name] = {
                    "name": name,
                    "description": body.get("description", ""),
                    "config": body.get("config") or {},
                    "devices": body.get("devices") or {},
                }
                return self.sync()
            case (_, ["profiles", name]):
                return self._profile_op(method, name, body)
            case ("GET", ["images"]):
                return self.sync(list(self.images.values()))
            case ("POST", ["images"]):
                source = body.get("source") or {}
                instance = self.instances.get(source.get("name"))
                if source.get("type") != "instance" or instance is None:
                    return self.error(404, "Instance not found")
                if instance["status"] != "Stopped":
                    return self.error(400, "The instance is running, stop it first")
                fingerprint = self.add_image(
                    (body.get("properties") or {}).get("description", ""),
                    [a["name"] for a in body.get("aliases") or []],
                )
                self.images[fingerprint]["cached"] = False
                self.published.append(json.loads(json.dumps(body)))
                return self.start_op(f"Publishing instance {instance['name']}")
            case ("POST", ["images", "aliases"]):
                name, target = body.get("name", ""), body.get("target", "")
                if target not in self.images:
                    return self.error(404, "Image not found")
                if any(a["name"] == name for i in self.images.values() for a in i["aliases"]):
                    return self.error(409, "Alias already exists")
                self.images[target]["aliases"].append(
                    {"name": name, "description": body.get("description", "")}
                )
                return self.sync()
            case ("DELETE", ["images", "aliases", *alias]):
                name = "/".join(alias)
                for image in self.images.values():
                    for entry in image["aliases"]:
                        if entry["name"] == name:
                            image["aliases"].remove(entry)
                            return self.sync()
                return self.error(404, "Alias not found")
            case ("GET", ["images", fingerprint]):
                image = self.images.get(fingerprint)
                return self.sync(image) if image else self.error(404, "Image not found")
            case ("DELETE", ["images", fingerprint]):
                if self.images.pop(fingerprint, None) is None:
                    return self.error(404, "Image not found")
                return self.start_op("Deleting image")
            case ("GET", ["operations"]):
                groups: dict[str, list] = {}
                for op in self.operations.values():
                    groups.setdefault(op["status"].lower(), []).append(op)
                return self.sync(groups)
            case ("GET", ["operations", op_id, "wait"]):
                op = self.operations.get(op_id)
                return self.sync(op) if op else self.error(404, "Operation not found")
            case ("DELETE", ["operations", op_id]):
                op = self.operations.get(op_id)
                if not op:
                    return self.error(404, "Operation not found")
                op.update(status="Cancelled", status_code=401, may_cancel=False)
                self.op_done.setdefault(op_id, threading.Event()).set()
                return self.sync()
            case ("GET", ["instances"]):
                return self.sync([self._full(i) for i in self.instances.values()])
            case ("POST", ["instances"]):
                name = body.get("name", "")
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name):
                    return self.error(400, f'Invalid instance name "{name}"')
                if name in self.instances:
                    return self.error(409, f'Instance "{name}" already exists')
                source = body.get("source") or {}
                if source.get("type") == "copy":
                    original = self.instances.get(source.get("source"))
                    if original is None:
                        return self.error(404, "Source instance not found")
                    copied = json.loads(json.dumps(original))
                    copied["name"] = name
                    copied["status"] = "Running" if body.get("start") else "Stopped"
                    if source.get("instance_only"):
                        copied["snapshots"] = []
                    self.instances[name] = copied
                    return self.start_op(f"Creating instance {name}")
                self.created.append(json.loads(json.dumps(body)))
                alias = body.get("source", {}).get("alias") or "local image"
                self.add_instance(name, "Running" if body.get("start") else "Stopped", image=alias)
                created = self.instances[name]
                created["profiles"] = body.get("profiles") or ["default"]
                created["config"].update(body.get("config") or {})
                created["description"] = body.get("description", "")
                return self.start_op(f"Creating instance {name}")
            case (_, ["instances", name, *rest]):
                instance = self.instances.get(name)
                if instance is None:
                    return self.error(404, f'Instance "{name}" not found')
                return self._instance(method, instance, rest, body)
            case ("GET", ["networks"]):
                return self.sync([self._network(n) for n in self.networks])
            case ("POST", ["networks"]):
                name = body.get("name", "")
                if name in self.networks:
                    return self.error(409, "Network already exists")
                if len(name) > 15:
                    return self.error(400, "Network name too long")
                config = dict(body.get("config") or {})
                for key in ("ipv4.address", "ipv6.address"):
                    if config.get(key) == "auto":
                        config[key] = "10.99.0.1/24" if key.startswith("ipv4") else "fd42:99::1/64"
                self.networks[name] = {
                    "name": name,
                    "type": body.get("type", "bridge"),
                    "managed": True,
                    "description": body.get("description", ""),
                    "config": config,
                    "status": "Created",
                }
                self.forwards[name] = {}
                return self.sync()
            case (_, ["networks", name, *rest]):
                if name not in self.networks:
                    return self.error(404, "Network not found")
                return self._network_op(method, name, rest, body)
            case ("GET", ["storage-pools"]):
                return self.sync(list(self.pools.values()))
            case (_, ["storage-pools", pool, "volumes", *rest]):
                if pool not in self.pools:
                    return self.error(404, "Storage pool not found")
                return self._volumes(method, pool, rest, body)
            case (_, ["storage-pools", pool, "buckets", *rest]):
                if pool not in self.pools:
                    return self.error(404, "Storage pool not found")
                return self._buckets(method, pool, rest, body)
        return self.error(404, "not found")

    def _wait(self, op_id, timeout):
        """Like Incus: block until the operation ends or the timeout passes."""
        with self.lock:
            self.calls.append(("GET", f"/1.0/operations/{op_id}/wait"))
            done = self.op_done.get(op_id)
        if done is not None and done.wait(min(timeout, 5)):
            # A waiter gets the operation as it ended, even if Incus drops it right after.
            with self.lock:
                op = self.operations.get(op_id) or self.ended.get(op_id)
        else:
            with self.lock:
                op = self.operations.get(op_id)
        return self.sync(op) if op else self.error(404, "Operation not found")

    def finish(self, op_id, err=""):
        """End a running operation, as Incus would."""
        with self.lock:
            op = self.operations[op_id]
            op.update(
                status="Failure" if err else "Success",
                status_code=400 if err else 200,
                err=err,
                may_cancel=False,
            )
            self.ended[op_id] = dict(op)
            self.op_done.setdefault(op_id, threading.Event()).set()

    def forget(self, op_id):
        """Drop an operation, as Incus does a few seconds after it ends."""
        with self.lock:
            self.operations.pop(op_id, None)
            self.op_done.setdefault(op_id, threading.Event()).set()

    # -- instance files ------------------------------------------------------

    def filesystem(self, name):
        """The instance's files: {path: {"type", "content", "mode", "uid", "gid"}}."""
        if name not in self.fs:
            entry = lambda kind, content=b"", mode=0o644: {  # noqa: E731
                "type": kind,
                "content": content,
                "mode": 0o755 if kind == "directory" else mode,
                "uid": 0,
                "gid": 0,
            }
            self.fs[name] = {
                "/": entry("directory"),
                "/etc": entry("directory"),
                "/etc/hostname": entry("file", f"{name}\n".encode()),
                "/etc/motd": entry("file", b"Welcome <b>here</b>\n"),
                "/root": entry("directory"),
                "/usr": entry("directory"),
                "/usr/bin": entry("directory"),
                "/usr/bin/true": entry("file", b"\x7fELF\0\0binary", 0o755),
                "/bin": entry("symlink", b"usr/bin", 0o777),
            }
        return self.fs[name]

    def files(self, method, name, file_path, body, headers):
        """The /1.0/instances/<name>/files endpoint: (status, body, headers)."""

        def error(code, message):
            data = json.dumps({"type": "error", "error_code": code, "error": message}).encode()
            return code, data, {"Content-Type": "application/json"}

        with self.lock:
            self.calls.append((method, f"/1.0/instances/{name}/files?path={file_path}"))
            if name not in self.instances:
                return error(404, "Instance not found")
            fs = self.filesystem(name)
            entry = fs.get(file_path)
            meta = (
                {
                    "X-Incus-Type": entry["type"],
                    "X-Incus-Uid": str(entry["uid"]),
                    "X-Incus-Gid": str(entry["gid"]),
                    "X-Incus-Mode": f"{entry['mode']:04o}",
                }
                if entry
                else {}
            )
            match method:
                case "HEAD" | "GET":
                    if entry is None:
                        return error(404, "Not found")
                    if entry["type"] == "directory":
                        prefix = file_path.rstrip("/") + "/"
                        children = sorted(
                            p[len(prefix) :]
                            for p in fs
                            if p.startswith(prefix) and p != prefix and "/" not in p[len(prefix) :]
                        )
                        data = json.dumps(
                            {"type": "sync", "status": "Success", "metadata": children}
                        ).encode()
                        return 200, data, {**meta, "Content-Type": "application/json"}
                    content = entry["content"]
                    return 200, content, {**meta, "Content-Type": "application/octet-stream"}
                case "POST":
                    parent = file_path.rsplit("/", 1)[0] or "/"
                    if fs.get(parent, {}).get("type") != "directory":
                        return error(404, "Parent folder does not exist")
                    kind = headers.get("X-Incus-Type", "file")
                    if entry and entry["type"] != kind:
                        return error(400, "Path exists with a different type")
                    fs[file_path] = {
                        "type": kind,
                        "content": body if kind == "file" else b"",
                        "mode": int(headers.get("X-Incus-Mode") or "644", 8),
                        "uid": int(headers.get("X-Incus-Uid") or 0),
                        "gid": int(headers.get("X-Incus-Gid") or 0),
                    }
                    self.writes.append((file_path, dict(headers)))
                    return 200, json.dumps({"type": "sync", "metadata": {}}).encode(), {}
                case "DELETE":
                    if entry is None:
                        return error(404, "Not found")
                    prefix = file_path.rstrip("/") + "/"
                    if entry["type"] == "directory" and any(p.startswith(prefix) for p in fs):
                        return error(400, "Directory not empty")
                    del fs[file_path]
                    return 200, json.dumps({"type": "sync", "metadata": {}}).encode(), {}
        return error(405, "Method not allowed")

    def add_image(self, description="", aliases=(), kind="container"):
        fingerprint = secrets.token_hex(32)
        self.images[fingerprint] = {
            "fingerprint": fingerprint,
            "type": kind,
            "architecture": "aarch64",
            "size": 123 * 1024 * 1024,
            "cached": True,
            "public": False,
            "created_at": _now(),
            "uploaded_at": _now(),
            "last_used_at": "0001-01-01T00:00:00Z",
            "properties": {"description": description, "os": "Debian", "release": "13"},
            "aliases": [{"name": a, "description": ""} for a in aliases],
            "update_source": None,
        }
        return fingerprint

    def _addresses(self, instance):
        if instance["status"] != "Running":
            return []
        return [f"10.0.3.{sorted(self.instances).index(instance['name']) + 10}"]

    def _network(self, name):
        used_by = [
            f"/1.0/instances/{i['name']}"
            for i in self.instances.values()
            if any(
                d.get("network") == name
                for d in {**i["expanded_devices"], **(i.get("devices") or {})}.values()
            )
        ]
        used_by += [
            f"/1.0/profiles/{p['name']}"
            for p in self.profiles.values()
            if any(d.get("network") == name for d in (p.get("devices") or {}).values())
        ]
        return {**self.networks[name], "used_by": used_by}

    def _network_op(self, method, name, rest, body):
        network = self.networks[name]
        managed = network.get("managed")
        forwards = self.forwards.setdefault(name, {})
        match (method, rest):
            case ("GET", []):
                return self.sync(self._network(name))
            case ("GET", ["state"]):
                return self.sync({"hwaddr": "10:66:6a:aa:bb:cc", "state": "up", "mtu": 1500})
            case ("GET", ["leases"]):
                if not managed:
                    return self.error(400, "Network is not managed")
                leases = [
                    {
                        "hostname": i["name"],
                        "address": a,
                        "hwaddr": "10:66:6a:00:00:01",
                        "type": "dynamic",
                    }
                    for i in self.instances.values()
                    for a in self._addresses(i)
                ]
                return self.sync(leases)
            case ("GET", ["forwards"]):
                return self.sync(list(forwards.values()))
            case ("POST", ["forwards"]):
                listen = body.get("listen_address", "")
                if listen in forwards:
                    return self.error(409, "A forward for that address exists")
                if listen.startswith("10.0.3."):
                    return self.error(400, "Listen address cannot be in the network's subnet")
                forwards[listen] = {**body, "location": ""}
                return self.sync()
            case ("GET", ["forwards", listen]):
                found = forwards.get(listen)
                return self.sync(found) if found else self.error(404, "Forward not found")
            case ("PUT", ["forwards", listen]):
                if listen not in forwards:
                    return self.error(404, "Forward not found")
                forwards[listen] = {**body, "listen_address": listen, "location": ""}
                return self.sync()
            case ("DELETE", ["forwards", listen]):
                if forwards.pop(listen, None) is None:
                    return self.error(404, "Forward not found")
                return self.sync()
        if not managed:
            return self.error(400, "Only managed networks can be modified")
        match (method, rest):
            case ("PUT", []):
                network["config"] = body.get("config") or {}
                network["description"] = body.get("description", "")
                return self.sync()
            case ("POST", []):
                new_name = body.get("name", "")
                if self._network(name)["used_by"]:
                    return self.error(400, "Network is currently in use")
                if new_name in self.networks:
                    return self.error(409, "Name already in use")
                self.networks[new_name] = {**self.networks.pop(name), "name": new_name}
                self.forwards[new_name] = self.forwards.pop(name, {})
                return self.sync()
            case ("DELETE", []):
                if self._network(name)["used_by"]:
                    return self.error(400, "The network is currently in use")
                del self.networks[name]
                return self.sync()
        return self.error(404, "not found")

    def _project(self, name):
        used_by = []
        if name == "default":
            used_by += [f"/1.0/instances/{i}" for i in self.instances]
            used_by += [f"/1.0/profiles/{p}" for p in self.profiles]
            used_by += [f"/1.0/images/{f}" for f in self.images]
        used_by += self.project_used_by.get(name, [])
        return {**self.projects[name], "used_by": used_by}

    def _project_op(self, method, name, rest, body):
        project = self.projects[name]
        match (method, rest):
            case ("GET", []):
                return self.sync(self._project(name))
            case ("GET", ["state"]):
                limits = project.get("config") or {}
                instances = len(self._project(name)["used_by"]) if name == "default" else 0

                def limit(key):
                    value = limits.get(f"limits.{key}")
                    return int(value) if value and value.isdigit() else -1

                return self.sync(
                    {
                        "resources": {
                            "instances": {"Limit": limit("instances"), "Usage": instances},
                            "containers": {"Limit": limit("containers"), "Usage": instances},
                            "memory": {"Limit": -1, "Usage": 2 * 1024**3},
                            "processes": {"Limit": limit("processes"), "Usage": 40},
                        }
                    }
                )
            case ("PUT", []):
                changed = {
                    k
                    for k in set(project["config"]) | set(body.get("config") or {})
                    if k.startswith("features.")
                    and project["config"].get(k) != (body.get("config") or {}).get(k)
                }
                if changed and self._project(name)["used_by"]:
                    return self.error(400, "Features can only be changed on empty projects")
                project["config"] = body.get("config") or {}
                project["description"] = body.get("description", "")
                return self.sync()
            case ("POST", []):
                if name == "default":
                    return self.error(403, "The 'default' project cannot be renamed")
                if self._project(name)["used_by"]:
                    return self.error(400, "Only empty projects can be renamed")
                new_name = body.get("name", "")
                self.projects[new_name] = {**self.projects.pop(name), "name": new_name}
                return self.sync()
            case ("DELETE", []):
                if name == "default":
                    return self.error(403, "The 'default' project cannot be deleted")
                if self._project(name)["used_by"]:
                    return self.error(400, "Only empty projects can be removed")
                del self.projects[name]
                return self.sync()
        return self.error(404, "not found")

    def _profile(self, name):
        used_by = [
            f"/1.0/instances/{i['name']}"
            for i in self.instances.values()
            if name in (i.get("profiles") or [])
        ]
        return {**self.profiles[name], "used_by": used_by}

    def _profile_op(self, method, name, body):
        if name not in self.profiles:
            return self.error(404, "Profile not found")
        match method:
            case "GET":
                return self.sync(self._profile(name))
            case "PUT":
                profile = self.profiles[name]
                for key in ("config", "description", "devices"):
                    profile[key] = body.get(key, {} if key != "description" else "")
                return self.sync()
            case "POST":
                new_name = body.get("name", "")
                if name == "default":
                    return self.error(403, "The 'default' profile cannot be renamed")
                if new_name in self.profiles:
                    return self.error(409, "Name already in use")
                profile = self.profiles.pop(name)
                profile["name"] = new_name
                self.profiles[new_name] = profile
                for instance in self.instances.values():
                    instance["profiles"] = [
                        new_name if p == name else p for p in instance.get("profiles") or []
                    ]
                return self.sync()
            case "DELETE":
                if name == "default":
                    return self.error(403, "The 'default' profile cannot be deleted")
                if self._profile(name)["used_by"]:
                    return self.error(400, "Profile is currently in use")
                del self.profiles[name]
                return self.sync()
        return self.error(404, "not found")

    def _instance(self, method, instance, rest, body):
        name = instance["name"]
        match (method, rest):
            case ("GET", []):
                return self.sync(self._full(instance))
            case ("POST", []):
                new_name = body.get("name", "")
                if instance["status"] != "Stopped":
                    return self.error(400, "Renaming of running instance not allowed")
                if new_name in self.instances:
                    return self.error(409, "Name already in use")
                del self.instances[name]
                instance["name"] = new_name
                self.instances[new_name] = instance
                return self.start_op(f"Renaming instance {name}")
            case ("GET", ["logs"]):
                return self.sync([f"/1.0/instances/{name}/logs/{f}" for f in self.LOGS])
            case ("GET", ["logs", filename]):
                if filename not in self.LOGS:
                    return self.error(404, "Log file not found")
                return 200, self.LOGS[filename]
            case ("GET", ["console"]):
                if instance["status"] != "Running":
                    return self.error(400, "Instance is not running")
                return 200, f"Debian GNU/Linux 13 {name} console\n<b>login:</b> "
            case ("PUT", []):
                if "restore" in body:
                    known = [s["name"] for s in instance["snapshots"]]
                    if body["restore"] not in known:
                        return self.error(404, "Snapshot not found")
                    return self.start_op(f"Restoring snapshot of {name}")
                self.puts.append(json.loads(json.dumps(body)))
                for key in ("config", "devices", "profiles", "description", "ephemeral"):
                    if key in body:
                        instance[key] = body[key]
                return self.start_op(f"Updating instance {name}")
            case ("DELETE", []):
                if instance["status"] != "Stopped":
                    return self.error(400, "Instance is running")
                del self.instances[name]
                return self.start_op(f"Deleting instance {name}")
            case ("PUT", ["state"]):
                action = body.get("action")
                current = instance["status"]
                if action == "start" and current != "Stopped":
                    return self.start_op(f"Starting {name}", err="The instance is already running")
                target = {
                    "start": "Running",
                    "restart": "Running",
                    "unfreeze": "Running",
                    "stop": "Stopped",
                    "freeze": "Frozen",
                }.get(action)
                if target is None:
                    return self.error(400, f"Unknown action {action}")
                instance["status"] = target
                return self.start_op(f"{action.title()}ing instance {name}")
            case ("POST", ["exec"]):
                if instance["status"] != "Running":
                    return self.error(400, "Instance is not running")
                status, response = self.start_op(f"Executing command in {name}")
                op = response["metadata"]
                op.update(status="Running", status_code=103)
                fds = {"0": secrets.token_hex(16), "control": secrets.token_hex(16)}
                op["metadata"] = {"fds": fds}
                self.sessions[op["id"]] = {
                    "request": body,
                    "data": fds["0"],
                    "control": fds["control"],
                    "stdin": b"",
                    "control_messages": [],
                }
                return status, response
            case ("GET", ["snapshots"]):
                return self.sync(instance["snapshots"])
            case ("POST", ["snapshots"]):
                snap = body.get("name", "")
                if any(s["name"] == snap for s in instance["snapshots"]):
                    return self.error(409, "Snapshot already exists")
                instance["snapshots"].append(
                    {"name": snap, "created_at": _now(), "expires_at": "0001-01-01T00:00:00Z"}
                )
                return self.start_op(f"Snapshotting {name}")
            case ("DELETE", ["snapshots", snap]):
                before = len(instance["snapshots"])
                instance["snapshots"] = [s for s in instance["snapshots"] if s["name"] != snap]
                if len(instance["snapshots"]) == before:
                    return self.error(404, "Snapshot not found")
                return self.start_op(f"Deleting snapshot {snap}")
        return self.error(404, "not found")

    def _new_key(self, name, body):
        return {
            "name": name,
            "role": body.get("role") or "read-only",
            "description": body.get("description", ""),
            "access-key": body.get("access-key") or secrets.token_hex(10).upper(),
            "secret-key": body.get("secret-key") or secrets.token_urlsafe(30),
        }

    BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")

    def _buckets(self, method, pool, rest, body):
        buckets = self.buckets.setdefault(pool, {})
        if self.pools[pool]["driver"] in self.no_bucket_drivers:
            return self.error(400, f"Storage pool does not support buckets ({pool})")
        match (method, rest):
            case ("GET", []):
                return self.sync([b["bucket"] for b in buckets.values()])
            case ("POST", []):
                name = body.get("name", "")
                if not self.BUCKET_NAME.fullmatch(name):
                    return self.error(400, f"Invalid bucket name {name!r}")
                if name in buckets:
                    return self.error(409, "A bucket for that name already exists")
                bucket = {
                    "name": name,
                    "description": body.get("description", ""),
                    "config": body.get("config") or {},
                    "s3_url": f"https://127.0.0.1:8555/{name}",
                    "location": "none",
                }
                admin = self._new_key("admin", {"role": "admin"})
                buckets[name] = {"bucket": bucket, "keys": {"admin": admin}}
                return self.sync(admin)
            case (_, [name, *more]):
                entry = buckets.get(name)
                if entry is None:
                    return self.error(404, "Storage bucket not found")
                match (method, more):
                    case ("GET", []):
                        return self.sync(entry["bucket"])
                    case ("PUT", []):
                        self.puts.append(json.loads(json.dumps(body)))
                        entry["bucket"]["description"] = body.get("description", "")
                        entry["bucket"]["config"] = body.get("config") or {}
                        return self.sync()
                    case ("DELETE", []):
                        del buckets[name]
                        return self.sync()
                    case ("GET", ["keys"]):
                        return self.sync(list(entry["keys"].values()))
                    case ("POST", ["keys"]):
                        key_name = body.get("name", "")
                        if key_name in entry["keys"]:
                            return self.error(409, "A bucket key for that name already exists")
                        key = self._new_key(key_name, body)
                        entry["keys"][key_name] = key
                        return self.sync(key)
                    case (_, ["keys", key_name]):
                        key = entry["keys"].get(key_name)
                        if key is None:
                            return self.error(404, "Storage bucket key not found")
                        if method == "GET":
                            return self.sync(key)
                        if method == "PUT":
                            self.puts.append(json.loads(json.dumps(body)))
                            # Like Incus: anything left out is generated anew.
                            entry["keys"][key_name] = self._new_key(key_name, body)
                            return self.sync()
                        if method == "DELETE":
                            del entry["keys"][key_name]
                            return self.sync()
        return self.error(404, "not found")

    # -- terminal websockets ----------------------------------------------

    def subscribe(self, ws):
        """Serve an /1.0/events websocket until the client goes away."""
        with self.lock:
            self.subscribers.append(ws)
        try:
            while ws.recv() is not None:
                pass
        finally:
            with self.lock:
                if ws in self.subscribers:
                    self.subscribers.remove(ws)

    def emit(self, event_type="lifecycle", **metadata):
        """Send an event to every /1.0/events subscriber."""
        event = {
            "type": event_type,
            "timestamp": _now(),
            "project": "default",
            "metadata": metadata,
        }
        with self.lock:
            listeners = list(self.subscribers)
        for ws in listeners:
            ws.send(WS_TEXT, json.dumps(event).encode())

    def websocket_session(self, raw_path):
        """(session, secret) for an operation websocket URL, or (None, secret)."""
        parts = urlsplit(raw_path)
        segments = parts.path.strip("/").split("/")
        secret = parse_qs(parts.query).get("secret", [""])[0]
        session = self.sessions.get(segments[2]) if len(segments) == 4 else None
        if session is None or secret not in (session["data"], session["control"]):
            return None, secret
        return session, secret

    def websocket(self, raw_path, ws):
        """Serve one operation websocket. The fake shell echoes its input and ends on "exit"."""
        session, secret = self.websocket_session(raw_path)
        while True:
            frame = ws.recv()
            if frame is None:
                return
            opcode, payload = frame
            if secret == session["control"]:
                session["control_messages"].append(json.loads(payload))
                continue
            session["stdin"] += payload
            ws.send(WS_BINARY, payload)
            if b"exit" in payload:
                ws.send(WS_CLOSE, struct.pack(">H", 1000))
                return


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_TEXT, WS_BINARY, WS_CLOSE, WS_PING, WS_PONG = 0x1, 0x2, 0x8, 0x9, 0xA


class _WebSocket:
    """Just enough of RFC 6455 for the server side of a test."""

    def __init__(self, rfile, wfile):
        self.rfile, self.wfile = rfile, wfile

    def send(self, opcode, payload=b""):
        header = bytes([0x80 | opcode])
        if len(payload) < 126:
            header += bytes([len(payload)])
        elif len(payload) < 65536:
            header += bytes([126]) + struct.pack(">H", len(payload))
        else:
            header += bytes([127]) + struct.pack(">Q", len(payload))
        try:
            self.wfile.write(header + payload)
            self.wfile.flush()
        except OSError:
            pass

    def recv(self):
        """The next text or binary message as (opcode, payload); None once the peer closes."""
        while True:
            try:
                head = self.rfile.read(2)
                if len(head) < 2:
                    return None
                opcode, length = head[0] & 0x0F, head[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self.rfile.read(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self.rfile.read(8))[0]
                mask = self.rfile.read(4) if head[1] & 0x80 else b""
                payload = self.rfile.read(length)
            except OSError:
                return None
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == WS_CLOSE:
                self.send(WS_CLOSE, payload[:2])
                return None
            if opcode == WS_PING:
                self.send(WS_PONG, payload)
            elif opcode in (WS_TEXT, WS_BINARY):
                return opcode, payload


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _accept(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        return _WebSocket(self.rfile, self.wfile)

    def _upgrade(self):
        if urlsplit(self.path).path == "/1.0/events":
            self.server.fake.subscribe(self._accept())
            return
        if self.server.fake.websocket_session(self.path)[0] is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        self.server.fake.websocket(self.path, _WebSocket(self.rfile, self.wfile))

    def _serve_file(self, name):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        status, data, headers = self.server.fake.files(
            self.command, name, query.get("path", ""), body, self.headers
        )
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(headers.get("Content-Length", len(data))))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def _serve(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._upgrade()
        parts = urlsplit(self.path).path.split("/")
        if len(parts) == 5 and parts[2] == "instances" and parts[4] == "files":
            return self._serve_file(unquote(parts[3]))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        peer = None
        if isinstance(self.connection, ssl.SSLSocket):
            peer = self.connection.getpeercert(binary_form=True)
        status, payload = self.server.fake.handle(self.command, self.path, body, peer)
        if isinstance(payload, str):  # a log file
            data, kind = payload.encode(), "text/plain"
        else:
            data, kind = json.dumps(payload).encode(), "application/json"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _serve

    def log_message(self, *args):  # keep test output quiet
        pass


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class _TCPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # clients hanging up is expected
        pass


def serve_unix(fake: FakeIncus, socket_path: str):
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = _UnixServer(socket_path, _Handler)
    server.fake = fake
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return server


def serve_tls(fake: FakeIncus, crt: str, key: str, client_ca: str | None = None):
    """Serve on 127.0.0.1 with TLS. ``client_ca`` is a client certificate to accept."""
    server = _TCPServer(("127.0.0.1", 0), _Handler)
    server.fake = fake
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(crt, key)
    if client_ca:
        context.verify_mode = ssl.CERT_OPTIONAL
        context.load_verify_locations(client_ca)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return server


def demo() -> FakeIncus:
    fake = FakeIncus()
    fake.add_instance("web-1")
    fake.add_instance("db-1", image="Ubuntu 26.04")
    fake.add_instance("scratch", status="Stopped")
    fake.add_pool("archive", "dir")
    fake.add_image("Web base (Debian 13)", ["web-base"])
    fake.add_volume("default", "site-data", config={"size": "10GiB"}, description="Web content")
    fake.add_bucket("default", "media", config={"size": "10GiB"}, description="Photos and video")
    fake.add_bucket("archive", "backups")
    return fake


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake-incus.sock"  # noqa: S108
    serve_unix(demo(), path)
    print(f"Fake Incus listening on {path}. Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
