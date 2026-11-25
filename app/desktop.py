"""The server the desktop app runs: ``python -m app.desktop``.

The Electron runtime (desktop/main.js) starts this with two environment
variables, ``INCUS_CONSOLE_DESKTOP_KEY`` (a random key for this launch) and
``INCUS_CONSOLE_DATA_DIR``. It listens on a free port on 127.0.0.1 and prints
``LISTENING <port>`` once it is ready, the line the runtime waits for.
"""

from __future__ import annotations

import os
import sys

from werkzeug.serving import make_server

from . import create_app

READY = "LISTENING"


def main() -> None:
    key = os.environ.get("INCUS_CONSOLE_DESKTOP_KEY", "")
    if len(key) < 32:
        sys.exit("INCUS_CONSOLE_DESKTOP_KEY must be set to a random key of 32 characters or more.")
    settings = {"SERVER_MODE": False, "DESKTOP_KEY": key}
    if os.environ.get("INCUS_CONSOLE_DATA_DIR"):
        settings["DATA_DIR"] = os.environ["INCUS_CONSOLE_DATA_DIR"]
    app = create_app(settings)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    print(f"{READY} {server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
