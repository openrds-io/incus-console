"""The browser terminal. Tests that need simple-websocket or websocket-client skip without them."""

import json
import os
import queue
import re
import socket
import threading

import pytest

from app import terminal
from app.certs import fingerprint, generate_self_signed
from app.incus import IncusClient, IncusConnectionError, IncusError
from app.servers import detected_sockets

from .fake_incus import serve_tls


def test_exec_request(fake, socket_path):
    fake.add_instance("web-1")
    client = IncusClient(socket_path=socket_path)
    session = client.exec_interactive("web-1", ["su", "-l"], cols=100, rows=30)
    sent = fake.sessions[session["id"]]["request"]
    assert sent["command"] == ["su", "-l"]
    assert sent["interactive"] is True and sent["wait-for-websocket"] is True
    assert (sent["width"], sent["height"]) == (100, 30)
    assert sent["environment"]["TERM"] == "xterm-256color"
    assert session["data"] != session["control"]


def test_exec_needs_a_running_instance(fake, socket_path):
    fake.add_instance("idle", status="Stopped")
    with pytest.raises(IncusError, match="not running"):
        IncusClient(socket_path=socket_path).exec_interactive("idle", ["su", "-l"])


def test_terminal_page(browser, sid, fake):
    fake.add_instance("web-1")
    fake.add_instance("idle", status="Stopped")
    page = browser.get(f"/servers/{sid}/instances/web-1/terminal").get_data(as_text=True)
    assert f'data-ws="/servers/{sid}/instances/web-1/terminal/ws"' in page
    idle = browser.get(f"/servers/{sid}/instances/idle/terminal").get_data(as_text=True)
    assert "Start it to open a terminal" in idle and "data-ws" not in idle
    listing = browser.get(f"/servers/{sid}/instances").get_data(as_text=True)
    assert listing.count(">Terminal</a>") == 1  # only for the running container


def test_plain_request_to_websocket_route_is_not_served(browser, sid, fake):
    fake.add_instance("web-1")
    assert browser.get(f"/servers/{sid}/instances/web-1/terminal/ws").status_code in (400, 404)


def test_colima_profiles_are_detected(app, short_dir):
    profile = f"{short_dir}/.colima/openrds"
    os.makedirs(profile)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(f"{profile}/incus.sock")
    app.config["LOCAL_SOCKETS"] = [f"{short_dir}/.colima/*/incus.sock", f"{short_dir}/missing"]
    with app.app_context():
        assert detected_sockets() == [{"path": f"{profile}/incus.sock", "name": "Colima openrds"}]
    listener.close()


class StubBrowser:
    """Stands in for simple-websocket's Server: receive() blocks, close() ends it."""

    def __init__(self):
        self.inbox = queue.Queue()
        self.sent = []
        self.closed = threading.Event()

    def type(self, **message):
        self.inbox.put(json.dumps(message))

    def receive(self):
        message = self.inbox.get()
        if message is None:
            raise ConnectionError("closed")
        return message

    def send(self, data):
        self.sent.append(data)

    def close(self):
        self.closed.set()
        self.inbox.put(None)


def _run_shell(client, fake):
    """Type into a fake shell through pump() and return what the browser saw."""
    session = client.exec_interactive("web-1", ["su", "-l"])
    data = client.websocket(session["id"], session["data"])
    control = client.websocket(session["id"], session["control"])
    stub = StubBrowser()
    stub.type(t="r", c=120, r="40")
    stub.type(t="i", d="echo héllo\n")
    stub.inbox.put("not json")
    stub.inbox.put(b"binary frames from the browser are ignored")
    stub.type(t="i", d="exit\n")
    worker = threading.Thread(target=terminal.pump, args=(stub, data, control))
    worker.start()
    worker.join(10)
    assert not worker.is_alive()
    recorded = fake.sessions[session["id"]]
    assert recorded["stdin"] == "echo héllo\nexit\n".encode()
    assert recorded["control_messages"] == [
        {"command": "window-resize", "args": {"width": "120", "height": "40"}}
    ]
    assert stub.sent == ["echo héllo\n".encode(), b"exit\n", '{"t": "x"}']
    assert stub.closed.is_set()


def test_pump_over_unix_socket(fake, socket_path):
    pytest.importorskip("websocket")
    fake.add_instance("web-1")
    _run_shell(IncusClient(socket_path=socket_path), fake)


def test_pump_over_pinned_tls(fake, tmp_path):
    pytest.importorskip("websocket")
    fake.add_instance("web-1")
    server_crt, server_key = tmp_path / "server.crt", tmp_path / "server.key"
    client_crt, client_key = tmp_path / "client.crt", tmp_path / "client.key"
    generate_self_signed("fake-incus", server_crt, server_key, server=True)
    generate_self_signed("incus-console", client_crt, client_key)
    server = serve_tls(fake, str(server_crt), str(server_key), client_ca=str(client_crt))
    try:
        client = IncusClient(
            url=f"https://127.0.0.1:{server.server_address[1]}",
            cert=str(client_crt),
            key=str(client_key),
            fingerprint=fingerprint(server_crt),
        )
        _run_shell(client, fake)
    finally:
        server.shutdown()
        server.server_close()


def test_websocket_with_wrong_secret_is_refused(fake, socket_path):
    pytest.importorskip("websocket")
    fake.add_instance("web-1")
    client = IncusClient(socket_path=socket_path)
    session = client.exec_interactive("web-1", ["su", "-l"])
    with pytest.raises(IncusConnectionError, match="terminal connection"):
        client.websocket(session["id"], "wrong")


def test_terminal_end_to_end(app, browser, sid, fake):
    """A real websocket through the Flask app to the fake shell and back."""
    pytest.importorskip("simple_websocket")
    websocket = pytest.importorskip("websocket")
    from werkzeug.serving import make_server

    fake.add_instance("web-1")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"127.0.0.1:{server.server_port}/servers/{sid}/instances"
    try:
        url = f"ws://{base}/web-1/terminal/ws?cols=90&rows=20"
        ws = websocket.create_connection(url, timeout=5)
        ws.send(json.dumps({"t": "i", "d": "hello"}))
        assert ws.recv() == b"hello"
        ws.send(json.dumps({"t": "i", "d": "exit"}))
        assert ws.recv() == b"exit"
        assert json.loads(ws.recv()) == {"t": "x"}
        [session] = fake.sessions.values()
        assert (session["request"]["width"], session["request"]["height"]) == (90, 20)

        # A page from another site may not open the terminal.
        with pytest.raises(websocket.WebSocketException):
            websocket.create_connection(
                f"ws://{base}/web-1/terminal/ws", origin="https://evil.example", timeout=5
            )

        # A problem starting the shell reaches the browser as a message.
        ws = websocket.create_connection(f"ws://{base}/ghost/terminal/ws", timeout=5)
        problem = json.loads(ws.recv())
        assert problem["t"] == "e" and re.search("not found", problem["m"])
    finally:
        server.shutdown()
