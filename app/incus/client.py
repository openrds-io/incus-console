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
        query = {"project": self.project, **(params or {})}
        target = f"{url_path}?{urlencode(query)}"
        headers = {"Accept": "application/json", "User-Agent": "incus-console"}
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
        try:
            data = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise IncusError(response.status, "Incus sent a response that is not JSON.") from exc
        if response.status >= 400 or data.get("type") == "error":
            raise IncusError(
                data.get("error_code") or response.status,
                data.get("error") or f"Incus returned HTTP {response.status}",
            )
        return data

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
        result = self.request(
            "GET",
            path("operations", op_id, "wait"),
            params={"timeout": timeout},
            timeout=timeout + 10,
        )
        op = result.get("metadata") or {}
        code = op.get("status_code")
        if code in (OP_FAILURE, OP_CANCELLED) or op.get("err"):
            raise IncusError(code or OP_FAILURE, op.get("err") or "The operation failed.")
        return code == OP_SUCCESS

    # -- server ------------------------------------------------------------

    def server_info(self) -> dict:
        return self.get()

    def projects(self) -> list:
        return self.get("projects", recursion=1) or []

    def profiles(self) -> list:
        return self.get("profiles", recursion=1) or []

    def images(self) -> list:
        return self.get("images", recursion=1) or []

    def operations(self) -> list:
        """All operations of the project as one list, newest first."""
        grouped = self.get("operations", recursion=1) or {}
        if isinstance(grouped, dict):
            ops = [op for group in grouped.values() for op in group]
        else:
            ops = list(grouped)
        return sorted(ops, key=lambda op: op.get("created_at") or "", reverse=True)

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

    def create_snapshot(self, name: str, snapshot: str) -> dict:
        body = {"name": snapshot, "stateful": False}
        return self.request("POST", path("instances", name, "snapshots"), body=body)

    def delete_snapshot(self, name: str, snapshot: str) -> dict:
        return self.request("DELETE", path("instances", name, "snapshots", snapshot))

    def restore_snapshot(self, name: str, snapshot: str) -> dict:
        return self.request("PUT", path("instances", name), body={"restore": snapshot})

    # -- storage -----------------------------------------------------------

    def pools(self) -> list:
        return self.get("storage-pools", recursion=1) or []

    def buckets(self, pool: str) -> list:
        return self.get("storage-pools", pool, "buckets", recursion=1) or []

    def bucket(self, pool: str, name: str) -> dict:
        return self.get("storage-pools", pool, "buckets", name)

    def create_bucket(self, pool: str, payload: dict) -> dict:
        return self.request("POST", path("storage-pools", pool, "buckets"), body=payload)

    def delete_bucket(self, pool: str, name: str) -> dict:
        return self.request("DELETE", path("storage-pools", pool, "buckets", name))

    def bucket_keys(self, pool: str, bucket: str) -> list:
        return self.get("storage-pools", pool, "buckets", bucket, "keys", recursion=1) or []

    def create_bucket_key(self, pool: str, bucket: str, payload: dict) -> dict:
        url = path("storage-pools", pool, "buckets", bucket, "keys")
        return self.request("POST", url, body=payload)

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
