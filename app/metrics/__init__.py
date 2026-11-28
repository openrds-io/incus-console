"""History of what instances use: sampled from Incus, kept in metrics.db, drawn as graphs.

One collector per console: a thread in whichever process takes the lock file
first (one of gunicorn's workers, or the desktop app). Data is kept at three
resolutions and thinned as it ages; see METRICS_* in app/config.py.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from . import store
from .collect import Collector

__all__ = ["start", "store"]


def _take_lock(path: Path):
    """An open file holding an exclusive lock, or None if another process has it."""
    handle = open(path, "a+")  # noqa: SIM115 - kept open for the life of the process
    try:
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # Windows
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def _run(app) -> None:
    interval = app.config["METRICS_INTERVAL"]
    lock = None
    while lock is None:
        lock = _take_lock(Path(app.config["DATA_DIR"]) / "metrics.lock")
        if lock is None:
            time.sleep(60)  # another process collects; take over if it stops
    with app.app_context():
        collector = Collector(store.connect(app.config["DATA_DIR"]), app.config)
        while True:
            began = time.monotonic()
            try:
                collector.cycle()
            except Exception:  # noqa: BLE001 - one bad cycle must not end collecting
                app.logger.exception("Collecting metrics failed")
            time.sleep(max(1.0, interval - (time.monotonic() - began)))


def start(app) -> None:
    """Start collecting in the background, unless testing or turned off."""
    if app.config.get("TESTING") or not app.config.get("METRICS_INTERVAL"):
        return
    threading.Thread(target=_run, args=(app,), name="metrics", daemon=True).start()
