"""Live updates: relay the server's Incus events to the browser as server-sent events.

Pages that show changing state (lists, detail pages, Operations) listen to this
stream and reload their content when something happens in the project. Each
open page holds one request (one thread) for as long as it is open.
"""

from __future__ import annotations

import json

from flask import Response, g

from ..incus import IncusError
from .scoped import bp

KEEP_ALIVE_SECONDS = 15


def _message(event: dict) -> str:
    metadata = event.get("metadata") or {}
    summary = {
        "type": event.get("type"),
        "action": metadata.get("action") or metadata.get("status") or "",
    }
    return f"data: {json.dumps(summary)}\n\n"


@bp.get("/events")
def events():
    client = g.incus

    def stream():
        import websocket

        try:
            upstream = client.events(idle=KEEP_ALIVE_SECONDS)
        except IncusError as error:
            yield f"event: problem\ndata: {json.dumps(error.message)}\n\n"
            return
        try:
            yield "retry: 5000\n\n"
            while True:
                try:
                    opcode, data = upstream.recv_data()
                except websocket.WebSocketTimeoutException:
                    # Also how we learn the browser left: this write fails.
                    yield ": keep-alive\n\n"
                    continue
                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    break
                if opcode != websocket.ABNF.OPCODE_TEXT:
                    continue
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                yield _message(event)
        except (OSError, websocket.WebSocketException):
            pass
        finally:
            upstream.close()

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
