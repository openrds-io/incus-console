"""An in-memory stand-in for the Incus API, for tests and for trying the UI.

Run a demo server with:  python -m tests.fake_incus /tmp/fake-incus.sock
"""

from __future__ import annotations

import datetime
import hashlib
import http.server
import json
import os
import re
import secrets
import socketserver
import ssl
import sys
import threading
import uuid
from urllib.parse import parse_qs, unquote, urlsplit


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeIncus:
    def __init__(self):
        self.lock = threading.Lock()
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
        self.calls: list[tuple[str, str]] = []
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
        body = {
            "type": "async",
            "status": "Operation created",
            "status_code": 100,
            "operation": f"/1.0/operations/{op_id}",
            "metadata": op,
        }
        return 202, body

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
        return {**instance, "state": state}

    # -- routing -----------------------------------------------------------

    def handle(self, method, raw_path, body, peer_cert=None):
        parts = urlsplit(raw_path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        segments = [unquote(s) for s in parts.path.strip("/").split("/")]
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
                        "environment": {"server_name": "fake", "server_version": "6.0"},
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
                return self.sync([{"name": "default", "description": "Default project"}])
            case ("GET", ["profiles"]):
                return self.sync([{"name": "default"}, {"name": "big"}])
            case ("GET", ["images"]):
                return self.sync([])
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
                return self.sync()
            case ("GET", ["instances"]):
                return self.sync([self._full(i) for i in self.instances.values()])
            case ("POST", ["instances"]):
                name = body.get("name", "")
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name):
                    return self.error(400, f'Invalid instance name "{name}"')
                if name in self.instances:
                    return self.error(409, f'Instance "{name}" already exists')
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
            case ("GET", ["storage-pools"]):
                return self.sync(list(self.pools.values()))
            case (_, ["storage-pools", pool, "buckets", *rest]):
                if pool not in self.pools:
                    return self.error(404, "Storage pool not found")
                return self._buckets(method, pool, rest, body)
        return self.error(404, "not found")

    def _instance(self, method, instance, rest, body):
        name = instance["name"]
        match (method, rest):
            case ("GET", []):
                return self.sync(self._full(instance))
            case ("PUT", []):
                if "restore" in body:
                    known = [s["name"] for s in instance["snapshots"]]
                    if body["restore"] not in known:
                        return self.error(404, "Snapshot not found")
                    return self.start_op(f"Restoring snapshot of {name}")
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

    def _new_key(self, name, role):
        return {
            "name": name,
            "role": role,
            "description": "",
            "access-key": secrets.token_hex(10).upper(),
            "secret-key": secrets.token_urlsafe(30),
        }

    def _buckets(self, method, pool, rest, body):
        buckets = self.buckets.setdefault(pool, {})
        match (method, rest):
            case ("GET", []):
                return self.sync([b["bucket"] for b in buckets.values()])
            case ("POST", []):
                name = body.get("name", "")
                if name in buckets:
                    return self.error(409, "Bucket already exists")
                bucket = {
                    "name": name,
                    "description": body.get("description", ""),
                    "config": body.get("config") or {},
                    "s3_url": f"https://127.0.0.1:8555/{name}",
                    "location": "none",
                }
                admin = self._new_key("admin", "admin")
                buckets[name] = {"bucket": bucket, "keys": {"admin": admin}}
                return self.sync(admin)
            case (_, [name, *more]):
                entry = buckets.get(name)
                if entry is None:
                    return self.error(404, "Storage bucket not found")
                match (method, more):
                    case ("GET", []):
                        return self.sync(entry["bucket"])
                    case ("DELETE", []):
                        del buckets[name]
                        return self.sync()
                    case ("GET", ["keys"]):
                        return self.sync(list(entry["keys"].values()))
                    case ("POST", ["keys"]):
                        key_name = body.get("name", "")
                        if key_name in entry["keys"]:
                            return self.error(409, "Key already exists")
                        key = self._new_key(key_name, body.get("role", "read-only"))
                        entry["keys"][key_name] = key
                        return self.sync(key)
                    case ("DELETE", ["keys", key_name]):
                        if entry["keys"].pop(key_name, None) is None:
                            return self.error(404, "Key not found")
                        return self.sync()
        return self.error(404, "not found")


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        peer = None
        if isinstance(self.connection, ssl.SSLSocket):
            peer = self.connection.getpeercert(binary_form=True)
        status, payload = self.server.fake.handle(self.command, self.path, body, peer)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

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
    return fake


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake-incus.sock"  # noqa: S108
    serve_unix(demo(), path)
    print(f"Fake Incus listening on {path}. Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
