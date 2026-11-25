"""The browser terminal: xterm.js <-> this app <-> an Incus exec session.

Same approach and wire format as openrds-io/ui's console. The browser sends
``{"t": "i", "d": keys}`` and ``{"t": "r", "c": cols, "r": rows}``; output goes
back as binary frames, then ``{"t": "x"}`` when the shell ends, or
``{"t": "e", "m": message}`` if it could not start (a browser cannot read a
failed websocket handshake).
"""

from __future__ import annotations

import json
import threading
from urllib.parse import urlsplit

from flask import Response, abort, request

from .incus import IncusError

MAX_SIZE = 1000  # columns or rows


def same_origin() -> bool:
    """Browsers always send Origin on a websocket; refuse other sites' pages."""
    origin = request.headers.get("Origin")
    return origin is None or urlsplit(origin).netloc == request.host


def _size(value, default: int) -> int:
    try:
        return min(max(int(value), 1), MAX_SIZE)
    except (TypeError, ValueError):
        return default


def pump(browser, data, control) -> None:
    """Relay between the browser's websocket and the Incus data and control websockets.

    ``browser`` needs ``receive()``, ``send()`` and ``close()`` (simple-websocket);
    ``data`` and ``control`` are websocket-client connections. Returns when either
    side ends, with both Incus websockets closed.
    """
    import websocket

    def down():  # Incus -> browser
        try:
            while True:
                opcode, payload = data.recv_data()
                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    break
                browser.send(bytes(payload))
        except Exception:  # noqa: BLE001, S110 - either side went away
            pass
        finally:
            try:
                browser.send(json.dumps({"t": "x"}))
                browser.close()
            except Exception:  # noqa: BLE001, S110 - the browser already left
                pass

    thread = threading.Thread(target=down, name="terminal-down", daemon=True)
    thread.start()
    try:
        while True:  # browser -> Incus
            message = browser.receive()
            if not isinstance(message, str):
                continue
            try:
                parsed = json.loads(message)
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue
            if parsed.get("t") == "i" and isinstance(parsed.get("d"), str):
                data.send_binary(parsed["d"].encode())
            elif parsed.get("t") == "r":
                size = {
                    "width": str(_size(parsed.get("c"), 80)),
                    "height": str(_size(parsed.get("r"), 24)),
                }
                control.send(json.dumps({"command": "window-resize", "args": size}))
    except Exception:  # noqa: BLE001, S110 - the browser left, or the shell ended
        pass
    finally:
        for upstream in (data, control):
            try:
                upstream.close()
            except Exception:  # noqa: BLE001, S110
                pass
        thread.join(5)


def relay(client, name: str, command: list[str]):
    """Accept the browser's websocket and attach it to a new shell in ``name``."""
    from simple_websocket import ConnectionClosed, Server

    if not same_origin():
        abort(403)
    cols = _size(request.args.get("cols"), 80)
    rows = _size(request.args.get("rows"), 24)
    browser = Server.accept(request.environ)
    opened = []
    try:
        session = client.exec_interactive(name, command, cols, rows)
        for secret in (session["data"], session["control"]):
            opened.append(client.websocket(session["id"], secret))
    except IncusError as error:
        for upstream in opened:
            upstream.close()
        try:
            browser.send(json.dumps({"t": "e", "m": error.message}))
            browser.close()
        except ConnectionClosed:
            pass
        return _done(browser)
    pump(browser, *opened)
    return _done(browser)


def _done(browser):
    """The response once a websocket is finished: the connection already belongs to it."""

    class WebSocketResponse(Response):
        def __call__(self, *args, **kwargs):
            if browser.mode == "werkzeug":
                raise ConnectionError()  # tells Werkzeug the socket was taken over
            if browser.mode == "gunicorn":
                raise StopIteration()
            return []

    return WebSocketResponse()
