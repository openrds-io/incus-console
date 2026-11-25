"""Live updates: the event stream and the pages that use it."""

import json
import threading
import time
import urllib.request

import pytest

from .test_app import text


def test_live_pages_are_marked(browser, sid, fake):
    fake.add_instance("web-1")
    marker = f'data-live="/servers/{sid}/events"'
    for url in ("instances", "instances/web-1", "operations", "volumes", "networks", "profiles"):
        assert marker in text(browser.get(f"/servers/{sid}/{url}")), url
    for url in ("create-instance", "instances/web-1/settings", "instances/web-1/terminal"):
        assert marker not in text(browser.get(f"/servers/{sid}/{url}")), url
    assert "data-live" not in text(browser.get("/servers"))


@pytest.fixture
def served(app):
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def read_events(response, count):
    found = []
    for raw in response:
        line = raw.decode().rstrip("\n")
        if line.startswith("data: "):
            found.append(json.loads(line[6:]))
            if len(found) == count:
                return found
    return found


def test_events_are_relayed(served, sid, fake):
    pytest.importorskip("websocket")
    with urllib.request.urlopen(f"{served}/servers/{sid}/events", timeout=10) as response:
        assert response.headers["Content-Type"].startswith("text/event-stream")
        assert response.readline() == b"retry: 5000\n"
        deadline = time.monotonic() + 5
        while not fake.subscribers and time.monotonic() < deadline:
            time.sleep(0.05)
        fake.emit("lifecycle", action="instance-started", source="/1.0/instances/web-1")
        fake.emit("operation", status="Running", description="Starting instance")
        assert read_events(response, 2) == [
            {"type": "lifecycle", "action": "instance-started"},
            {"type": "operation", "action": "Running"},
        ]


def test_event_stream_reports_unreachable_server(served, sid, fake, socket_path):
    pytest.importorskip("websocket")
    import os

    os.unlink(socket_path)
    with urllib.request.urlopen(f"{served}/servers/{sid}/events", timeout=10) as response:
        body = response.read().decode()
    assert body.startswith("event: problem\n")
    assert "Cannot open the event connection" in body
