"""A small synchronous client for the Incus REST API.

Uses only the standard library. Two transports: a local unix socket, or HTTPS
with a client certificate and the server certificate pinned by its SHA-256
fingerprint (Incus servers normally use self-signed certificates).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import socket
import ssl
from urllib.parse import quote, urlencode, urlsplit

API = "1.0"

OP_SUCCESS = 200
OP_FAILURE = 400
OP_CANCELLED = 401


class IncusError(Exception):
    """The server answered with an error, or an operation failed."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class IncusConnectionError(IncusError):
    """The server could not be reached or the TLS checks failed."""

    def __init__(self, message: str):
        super().__init__(0, message)


def path(*segments: str) -> str:
    """Build an API path, percent-encoding each segment."""
    return "/" + "/".join([API, *(quote(str(s), safe="") for s in segments)])


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, context, fingerprint, timeout):
        super().__init__(host, port, context=context, timeout=timeout)
        self._fingerprint = fingerprint

    def connect(self) -> None:
        super().connect()
        if not self._fingerprint:
            return
        got = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(got, self._fingerprint):
            self.close()
            raise IncusConnectionError(
                "The server certificate does not match the saved fingerprint."
            )


def normalize_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace(":", "").replace(" ", "").lower()


class IncusClient:
    def __init__(
        self,
        *,
        socket_path: str | None = None,
        url: str | None = None,
        cert: str | None = None,
        key: str | None = None,
        fingerprint: str | None = None,
        project: str = "default",
        timeout: float = 30,
    ):
        if bool(socket_path) == bool(url):
            raise ValueError("Give exactly one of socket_path or url")
        self.socket_path = socket_path
        self.url = url
        self.cert = cert
        self.key = key
        self.fingerprint = normalize_fingerprint(fingerprint)
        self.project = project
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        if self.socket_path:
            return _UnixConnection(self.socket_path, timeout)
        parts = urlsplit(self.url)
        if parts.scheme != "https" or not parts.hostname:
            raise IncusConnectionError("Remote servers need an https:// address.")
        if self.fingerprint:
            # The pinned fingerprint replaces CA and hostname verification.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
        if self.cert:
            context.load_cert_chain(self.cert, self.key)
        return _PinnedHTTPSConnection(
            parts.hostname,
            parts.port or 8443,
            context=context,
            fingerprint=self.fingerprint,
            timeout=timeout,
        )

    def request(
        self,
        method: str,
        url_path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Send one request and return the decoded response envelope."""
        status, raw = self._send(method, url_path, params=params, body=body, timeout=timeout)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise IncusError(status, "Incus sent a response that is not JSON.") from exc
        if status >= 400 or data.get("type") == "error":
            raise IncusError(
                data.get("error_code") or status,
                data.get("error") or f"Incus returned HTTP {status}",
            )
        return data

    def get_text(self, *segments: str, **params) -> str:
        """GET an endpoint that answers with plain text (log files)."""
        status, raw = self._send("GET", path(*segments), params=params)
        if status >= 400:
            try:
                error = json.loads(raw).get("error")
            except ValueError:
                error = None
            raise IncusError(status, error or f"Incus returned HTTP {status}")
        return raw.decode("utf-8", errors="replace")

    def _send(self, method, url_path, *, params=None, body=None, timeout=None):
        """Send one request; return (HTTP status, body bytes)."""
        query = {"project": self.project, **(params or {})}
        target = f"{url_path}?{urlencode(query)}"
        headers = {"User-Agent": "incus-console"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn = self._connect(timeout or self.timeout)
            try:
                conn.request(method, target, body=payload, headers=headers)
                response = conn.getresponse()
                raw = response.read()
            finally:
                conn.close()
        except IncusError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            where = self.socket_path or self.url
            raise IncusConnectionError(f"Cannot reach Incus at {where}: {exc}") from exc
        return response.status, raw

    # -- raw requests (instance files) ----------------------------------------

    def _open(self, method, url_path, *, params=None, body=None, headers=None, timeout=None):
        """Send a request and return (connection, response) with the body unread.

        The caller reads the response and closes the connection.
        """
        query = {"project": self.project, **(params or {})}
        target = f"{url_path}?{urlencode(query)}"
        all_headers = {"User-Agent": "incus-console", **(headers or {})}
        try:
            conn = self._connect(timeout or self.timeout)
            conn.request(method, target, body=body, headers=all_headers)
            return conn, conn.getresponse()
        except IncusError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            where = self.socket_path or self.url
            raise IncusConnectionError(f"Cannot reach Incus at {where}: {exc}") from exc

    @staticmethod
    def _error_from(status: int, raw: bytes) -> IncusError:
        try:
            message = json.loads(raw).get("error")
        except (ValueError, AttributeError):
            message = None
        return IncusError(status, message or f"Incus returned HTTP {status}")

    @staticmethod
    def file_info_from(headers) -> dict:
        """What the X-Incus-* headers say about a file."""

        def number(name):
            value = headers.get(name)
            return int(value) if value and value.lstrip("-").isdigit() else None

        mode = headers.get("X-Incus-Mode")
        return {
            "type": headers.get("X-Incus-Type", "file"),
            "uid": number("X-Incus-Uid"),
            "gid": number("X-Incus-Gid"),
            "mode": int(mode, 8) if mode and mode.isdigit() else None,
            "size": number("Content-Length"),
            "modified": headers.get("X-Incus-Modified") or headers.get("Last-Modified"),
        }

    def file_info(self, name: str, file_path: str) -> dict:
        conn, response = self._open(
            "HEAD", path("instances", name, "files"), params={"path": file_path}
        )
        try:
            raw = response.read()
        finally:
            conn.close()
        if response.status >= 400:
            if raw:
                raise self._error_from(response.status, raw)
            # HEAD answers usually carry no body, so no message from Incus.
            if response.status == 404:
                raise IncusError(404, f"{file_path} does not exist.")
            raise IncusError(response.status, f"Incus returned HTTP {response.status}")
        return self.file_info_from(response.headers)

    def list_directory(self, name: str, dir_path: str) -> list[str]:
        conn, response = self._open(
            "GET", path("instances", name, "files"), params={"path": dir_path}
        )
        try:
            raw = response.read()
        finally:
            conn.close()
        if response.status >= 400:
            raise self._error_from(response.status, raw)
        if response.headers.get("X-Incus-Type") != "directory":
            raise IncusError(400, f"{dir_path} is not a folder.")
        return sorted(json.loads(raw).get("metadata") or [])

    def open_file(self, name: str, file_path: str):
        """GET a file for streaming: returns (info, response, connection).

        Read the response in chunks, then close the connection.
        """
        conn, response = self._open(
            "GET", path("instances", name, "files"), params={"path": file_path}, timeout=300
        )
        if response.status >= 400:
            try:
                raise self._error_from(response.status, response.read())
            finally:
                conn.close()
        return self.file_info_from(response.headers), response, conn

    def write_file(
        self,
        name: str,
        file_path: str,
        data,
        length: int,
        *,
        mode: int | None = None,
        uid: int | None = None,
        gid: int | None = None,
        kind: str = "file",
    ) -> None:
        """Create or overwrite a file (data: bytes or a binary file object), or a folder."""
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(length),
            "X-Incus-Type": kind,
            "X-Incus-Write": "overwrite",
        }
        if mode is not None:
            headers["X-Incus-Mode"] = f"{mode:04o}"
        if uid is not None:
            headers["X-Incus-Uid"] = str(uid)
        if gid is not None:
            headers["X-Incus-Gid"] = str(gid)
        conn, response = self._open(
            "POST",
            path("instances", name, "files"),
            params={"path": file_path},
            body=data,
            headers=headers,
            timeout=600,
        )
        try:
            raw = response.read()
        finally:
            conn.close()
        if response.status >= 400:
            raise self._error_from(response.status, raw)

    def delete_file(self, name: str, file_path: str) -> None:
        conn, response = self._open(
            "DELETE", path("instances", name, "files"), params={"path": file_path}
        )
        try:
            raw = response.read()
        finally:
            conn.close()
        if response.status >= 400:
            raise self._error_from(response.status, raw)

    def get(self, *segments: str, **params) -> object:
        return self.request("GET", path(*segments), params=params).get("metadata")

    def wait(self, response: dict, timeout: int = 30) -> bool:
        """Wait for the operation in ``response``.

        Returns True when it finished, False when it is still running after
        ``timeout`` seconds. Raises IncusError when it failed.
        """
        if response.get("type") != "async":
            return True
        op_id = (response.get("metadata") or {}).get("id")
        if not op_id:
            return True
        op = self.wait_operation(op_id, timeout)
        code = op.get("status_code")
        if code in (OP_FAILURE, OP_CANCELLED) or op.get("err"):
            raise IncusError(code or OP_FAILURE, op.get("err") or "The operation failed.")
        return code == OP_SUCCESS

    # -- server ------------------------------------------------------------

    def server_info(self) -> dict:
        return self.get()

    def projects(self) -> list:
        return self.get("projects", recursion=1) or []

    # -- trust store -------------------------------------------------------

    def certificate(self, fingerprint: str) -> dict:
        return self.get("certificates", fingerprint)

    def add_certificate(self, payload: dict) -> dict:
        return self.request("POST", path("certificates"), body=payload)

    def update_certificate(self, fingerprint: str, payload: dict) -> dict:
        return self.request("PUT", path("certificates", fingerprint), body=payload)

    def delete_certificate(self, fingerprint: str) -> dict:
        return self.request("DELETE", path("certificates", fingerprint))

    def project_details(self, name: str) -> dict:
        return self.get("projects", name)

    def project_state(self, name: str) -> dict:
        return self.get("projects", name, "state") or {}

    def create_project(self, payload: dict) -> dict:
        return self.request("POST", path("projects"), body=payload)

    def update_project(self, name: str, payload: dict) -> dict:
        return self.request("PUT", path("projects", name), body=payload)

    def rename_project(self, name: str, new_name: str) -> dict:
        return self.request("POST", path("projects", name), body={"name": new_name})

    def delete_project(self, name: str) -> dict:
        return self.request("DELETE", path("projects", name))

    def profiles(self) -> list:
        return self.get("profiles", recursion=1) or []

    def profile(self, name: str) -> dict:
        return self.get("profiles", name)

    def create_profile(self, payload: dict) -> dict:
        return self.request("POST", path("profiles"), body=payload)

    def update_profile(self, name: str, payload: dict) -> dict:
        return self.request("PUT", path("profiles", name), body=payload)

    def rename_profile(self, name: str, new_name: str) -> dict:
        return self.request("POST", path("profiles", name), body={"name": new_name})

    def delete_profile(self, name: str) -> dict:
        return self.request("DELETE", path("profiles", name))

    def images(self) -> list:
        return self.get("images", recursion=1) or []

    def image(self, fingerprint: str) -> dict:
        return self.get("images", fingerprint)

    def delete_image(self, fingerprint: str) -> dict:
        return self.request("DELETE", path("images", fingerprint))

    def add_image_alias(self, fingerprint: str, alias: str, description: str = "") -> dict:
        body = {"name": alias, "target": fingerprint, "description": description}
        return self.request("POST", path("images", "aliases"), body=body)

    def delete_image_alias(self, alias: str) -> dict:
        return self.request("DELETE", path("images", "aliases", alias))

    def publish_instance(self, name: str, alias: str = "", description: str = "") -> dict:
        """Make an image from a stopped instance."""
        body = {
            "source": {"type": "instance", "name": name},
            "public": False,
            "properties": {"description": description} if description else {},
            "aliases": [{"name": alias}] if alias else [],
        }
        return self.request("POST", path("images"), body=body)

    def operations(self) -> list:
        """All operations of the project as one list, newest first."""
        grouped = self.get("operations", recursion=1) or {}
        if isinstance(grouped, dict):
            ops = [op for group in grouped.values() for op in group]
        else:
            ops = list(grouped)
        return sorted(ops, key=lambda op: op.get("created_at") or "", reverse=True)

    def wait_operation(self, op_id: str, timeout: int = 30) -> dict:
        """The operation once it ends, or as it is after ``timeout`` seconds."""
        result = self.request(
            "GET",
            path("operations", op_id, "wait"),
            params={"timeout": timeout},
            timeout=timeout + 10,
        )
        return result.get("metadata") or {}

    def cancel_operation(self, op_id: str) -> dict:
        return self.request("DELETE", path("operations", op_id))

    # -- instances ---------------------------------------------------------

    def instances(self) -> list:
        return self.get("instances", recursion=2) or []

    def instance(self, name: str) -> dict:
        return self.get("instances", name, recursion=1)

    def create_instance(self, payload: dict) -> dict:
        return self.request("POST", path("instances"), body=payload)

    def update_instance(self, name: str, payload: dict) -> dict:
        return self.request("PUT", path("instances", name), body=payload)

    def delete_instance(self, name: str) -> dict:
        return self.request("DELETE", path("instances", name))

    def instance_action(self, name: str, action: str, force: bool = False) -> dict:
        body = {"action": action, "timeout": 30, "force": force}
        return self.request("PUT", path("instances", name, "state"), body=body)

    def rename_instance(self, name: str, new_name: str) -> dict:
        return self.request("POST", path("instances", name), body={"name": new_name})

    def copy_instance(
        self, name: str, new_name: str, *, snapshots: bool = True, start: bool = False
    ) -> dict:
        body = {
            "name": new_name,
            "source": {"type": "copy", "source": name, "instance_only": not snapshots},
            "start": start,
        }
        return self.request("POST", path("instances"), body=body)

    def instance_log_files(self, name: str) -> list[str]:
        """Names of the instance's log files (lxc.log, lxc.conf, ...)."""
        urls = self.get("instances", name, "logs") or []
        return sorted(url.rstrip("/").rsplit("/", 1)[-1] for url in urls)

    def instance_log(self, name: str, filename: str) -> str:
        return self.get_text("instances", name, "logs", filename)

    def console_log(self, name: str) -> str:
        """What the instance has written to its console."""
        return self.get_text("instances", name, "console")

    def create_snapshot(self, name: str, snapshot: str) -> dict:
        body = {"name": snapshot, "stateful": False}
        return self.request("POST", path("instances", name, "snapshots"), body=body)

    def delete_snapshot(self, name: str, snapshot: str) -> dict:
        return self.request("DELETE", path("instances", name, "snapshots", snapshot))

    def restore_snapshot(self, name: str, snapshot: str) -> dict:
        return self.request("PUT", path("instances", name), body={"restore": snapshot})

    # -- terminal ----------------------------------------------------------

    def exec_interactive(self, name: str, command: list[str], cols: int = 80, rows: int = 24):
        """Start ``command`` on a pseudo-terminal in an instance.

        Incus waits for both websockets before running it. Returns the operation
        id and the two secrets: ``{"id": ..., "data": ..., "control": ...}``.
        """
        body = {
            "command": command,
            "environment": {"TERM": "xterm-256color"},
            "wait-for-websocket": True,
            "interactive": True,
            "width": cols,
            "height": rows,
        }
        response = self.request("POST", path("instances", name, "exec"), body=body)
        op = response.get("metadata") or {}
        fds = (op.get("metadata") or {}).get("fds") or {}
        if not op.get("id") or "0" not in fds or "control" not in fds:
            raise IncusError(500, "Incus did not offer a terminal session.")
        return {"id": op["id"], "data": fds["0"], "control": fds["control"]}

    def console_interactive(self, name: str, cols: int = 80, rows: int = 24):
        """Attach to an instance's text console.

        Returns ``{"id": ..., "data": ..., "control": ...}`` like exec_interactive.
        """
        body = {"type": "console", "width": cols, "height": rows}
        response = self.request("POST", path("instances", name, "console"), body=body)
        op = response.get("metadata") or {}
        fds = (op.get("metadata") or {}).get("fds") or {}
        if not op.get("id") or "0" not in fds or "control" not in fds:
            raise IncusError(500, "Incus did not offer a console session.")
        return {"id": op["id"], "data": fds["0"], "control": fds["control"]}

    def websocket(self, op_id: str, secret: str):
        """Open one of an operation's websockets over this client's transport.

        Returns a ``websocket.WebSocket`` (the websocket-client package).
        """
        query = urlencode({"secret": secret, "project": self.project})
        ws = self._websocket(f"{path('operations', op_id, 'websocket')}?{query}", "terminal")
        ws.sock.settimeout(None)  # an idle terminal must not time out
        return ws

    def events(self, types: tuple[str, ...] = ("operation", "lifecycle"), idle: float = 15):
        """Subscribe to the project's events; ``recv`` times out after ``idle`` seconds."""
        query = urlencode({"type": ",".join(types), "project": self.project})
        ws = self._websocket(f"{path('events')}?{query}", "event")
        ws.sock.settimeout(idle)
        return ws

    def _websocket(self, target: str, what: str):
        import websocket

        url = f"ws://incus{target}"
        sock = None
        try:
            conn = self._connect(self.timeout)
            conn.connect()
            # The websocket takes over the socket (unix, or TLS already pinned).
            sock, conn.sock = conn.sock, None
            ws = websocket.create_connection(
                url, socket=sock, suppress_origin=True, timeout=self.timeout
            )
        except IncusError:
            raise
        except (OSError, websocket.WebSocketException) as exc:
            if sock is not None:
                sock.close()
            raise IncusConnectionError(f"Cannot open the {what} connection: {exc}") from exc
        return ws

    # -- networks ----------------------------------------------------------

    def networks(self) -> list:
        return self.get("networks", recursion=1) or []

    def network(self, name: str) -> dict:
        return self.get("networks", name)

    def network_state(self, name: str) -> dict:
        return self.get("networks", name, "state") or {}

    def network_leases(self, name: str) -> list:
        return self.get("networks", name, "leases") or []

    def create_network(self, payload: dict) -> dict:
        return self.request("POST", path("networks"), body=payload)

    def update_network(self, name: str, payload: dict) -> dict:
        return self.request("PUT", path("networks", name), body=payload)

    def rename_network(self, name: str, new_name: str) -> dict:
        return self.request("POST", path("networks", name), body={"name": new_name})

    def delete_network(self, name: str) -> dict:
        return self.request("DELETE", path("networks", name))

    def network_forwards(self, name: str) -> list:
        return self.get("networks", name, "forwards", recursion=1) or []

    def network_forward(self, name: str, listen: str) -> dict:
        return self.get("networks", name, "forwards", listen)

    def create_network_forward(self, name: str, payload: dict) -> dict:
        return self.request("POST", path("networks", name, "forwards"), body=payload)

    def update_network_forward(self, name: str, listen: str, payload: dict) -> dict:
        return self.request("PUT", path("networks", name, "forwards", listen), body=payload)

    def delete_network_forward(self, name: str, listen: str) -> dict:
        return self.request("DELETE", path("networks", name, "forwards", listen))

    # -- storage -----------------------------------------------------------

    def pools(self) -> list:
        return self.get("storage-pools", recursion=1) or []

    def volumes(self, pool: str) -> list:
        """The pool's volumes of every type (custom, container, image, ...)."""
        return self.get("storage-pools", pool, "volumes", recursion=1) or []

    def volume(self, pool: str, name: str) -> dict:
        return self.get("storage-pools", pool, "volumes", "custom", name)

    def create_volume(self, pool: str, payload: dict) -> dict:
        return self.request("POST", path("storage-pools", pool, "volumes", "custom"), body=payload)

    def update_volume(self, pool: str, name: str, payload: dict) -> dict:
        url = path("storage-pools", pool, "volumes", "custom", name)
        return self.request("PUT", url, body=payload)

    def rename_volume(self, pool: str, name: str, new_name: str) -> dict:
        url = path("storage-pools", pool, "volumes", "custom", name)
        return self.request("POST", url, body={"name": new_name})

    def delete_volume(self, pool: str, name: str) -> dict:
        return self.request("DELETE", path("storage-pools", pool, "volumes", "custom", name))

    def volume_snapshots(self, pool: str, name: str) -> list:
        segments = ("storage-pools", pool, "volumes", "custom", name, "snapshots")
        return self.get(*segments, recursion=1) or []

    def create_volume_snapshot(self, pool: str, name: str, snapshot: str) -> dict:
        url = path("storage-pools", pool, "volumes", "custom", name, "snapshots")
        return self.request("POST", url, body={"name": snapshot})

    def delete_volume_snapshot(self, pool: str, name: str, snapshot: str) -> dict:
        url = path("storage-pools", pool, "volumes", "custom", name, "snapshots", snapshot)
        return self.request("DELETE", url)

    def restore_volume_snapshot(self, pool: str, name: str, snapshot: str) -> dict:
        url = path("storage-pools", pool, "volumes", "custom", name)
        return self.request("PUT", url, body={"restore": snapshot})

    def buckets(self, pool: str) -> list:
        return self.get("storage-pools", pool, "buckets", recursion=1) or []

    def bucket(self, pool: str, name: str) -> dict:
        return self.get("storage-pools", pool, "buckets", name)

    def create_bucket(self, pool: str, payload: dict) -> dict:
        """Create a bucket. Incus answers with its admin key, the one time it shows it."""
        url = path("storage-pools", pool, "buckets")
        return self.request("POST", url, body=payload).get("metadata") or {}

    def update_bucket(self, pool: str, name: str, payload: dict) -> dict:
        """Replace a bucket's description and config (``size``)."""
        return self.request("PUT", path("storage-pools", pool, "buckets", name), body=payload)

    def delete_bucket(self, pool: str, name: str) -> dict:
        return self.request("DELETE", path("storage-pools", pool, "buckets", name))

    def bucket_keys(self, pool: str, bucket: str) -> list:
        return self.get("storage-pools", pool, "buckets", bucket, "keys", recursion=1) or []

    def bucket_key(self, pool: str, bucket: str, key: str) -> dict:
        return self.get("storage-pools", pool, "buckets", bucket, "keys", key)

    def create_bucket_key(self, pool: str, bucket: str, payload: dict) -> dict:
        """Create a key; returns it with its access and secret keys."""
        url = path("storage-pools", pool, "buckets", bucket, "keys")
        return self.request("POST", url, body=payload).get("metadata") or {}

    def update_bucket_key(self, pool: str, bucket: str, key: str, payload: dict) -> dict:
        """Replace a key's role, description, access key and secret key."""
        url = path("storage-pools", pool, "buckets", bucket, "keys", key)
        return self.request("PUT", url, body=payload)

    def delete_bucket_key(self, pool: str, bucket: str, key: str) -> dict:
        return self.request("DELETE", path("storage-pools", pool, "buckets", bucket, "keys", key))


# -- trust tokens ----------------------------------------------------------


def decode_trust_token(token: str) -> dict:
    """Decode a token made by ``incus config trust add <name>``."""
    text = "".join(token.split())
    try:
        data = json.loads(base64.b64decode(text + "=" * (-len(text) % 4)))
    except ValueError as exc:
        raise IncusError(400, "That does not look like an Incus trust token.") from exc
    if not isinstance(data, dict) or not data.get("fingerprint") or not data.get("addresses"):
        raise IncusError(400, "The trust token has no server address or fingerprint.")
    return data


def enroll_with_token(token: str, cert: str, key: str, timeout: float = 15) -> dict:
    """Add our client certificate to a server's trust store using a token.

    Returns ``{"url": ..., "fingerprint": ..., "name": ...}`` for the address
    that worked. The server certificate must match the token's fingerprint.
    """
    info = decode_trust_token(token)
    last_error: IncusError | None = None
    for address in info["addresses"]:
        url = f"https://{address}"
        client = IncusClient(
            url=url, cert=cert, key=key, fingerprint=info["fingerprint"], timeout=timeout
        )
        try:
            client.request(
                "POST",
                path("certificates"),
                body={"type": "client", "trust_token": "".join(token.split())},
            )
        except IncusConnectionError as exc:
            last_error = exc
            continue
        return {
            "url": url,
            "fingerprint": normalize_fingerprint(info["fingerprint"]),
            "name": info.get("client_name") or address,
        }
    raise last_error or IncusConnectionError("The trust token lists no reachable address.")
