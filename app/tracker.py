"""Keep the outcome of operations the console handed off to Operations.

When an action is still running after ACTION_WAIT seconds, the page moves on
and a background thread waits for the operation instead. Incus forgets a
finished operation within seconds, so the thread records how it ended; the
next page of that server shows it once as a message, and the Operations page
lists recent ones.
"""

from __future__ import annotations

import threading
import time

from .db import connect
from .incus import IncusConnectionError, IncusError
from .incus.client import OP_CANCELLED, OP_FAILURE, OP_SUCCESS

WAIT_SECONDS = 60  # one long-poll to Incus
GIVE_UP_SECONDS = 4 * 3600
RETRY_SECONDS = 10  # after losing the connection to Incus
FINAL = {OP_SUCCESS: "Success", OP_FAILURE: "Failure", OP_CANCELLED: "Cancelled"}

_watching: set[tuple[str, int]] = set()  # (data dir, row id) being watched
_lock = threading.Lock()


def _finish(data_dir: str, row_id: int, status: str, error: str | None = None) -> None:
    with connect(data_dir) as db:
        db.execute(
            "UPDATE tracked_operations SET status = ?, error = ?, "
            "finished_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Running'",
            (status, error, row_id),
        )


def _watch(data_dir: str, client, row_id: int, op_id: str) -> None:
    deadline = time.monotonic() + GIVE_UP_SECONDS
    try:
        while time.monotonic() < deadline:
            try:
                op = client.wait_operation(op_id, WAIT_SECONDS)
            except IncusConnectionError:
                time.sleep(RETRY_SECONDS)
                continue
            except IncusError as error:
                if error.status == 404:
                    _finish(data_dir, row_id, "Unknown", "Incus no longer knows this operation.")
                else:
                    _finish(data_dir, row_id, "Unknown", error.message)
                return
            code = op.get("status_code")
            if code in FINAL:
                status = "Failure" if op.get("err") and code == OP_SUCCESS else FINAL[code]
                _finish(data_dir, row_id, status, op.get("err") or None)
                return
        _finish(data_dir, row_id, "Unknown", "Stopped watching after four hours.")
    finally:
        with _lock:
            _watching.discard((data_dir, row_id))


def watch(data_dir: str, client, row_id: int, op_id: str) -> None:
    """Start a watcher for a tracked operation unless one is running in this process."""
    with _lock:
        if (data_dir, row_id) in _watching:
            return
        _watching.add((data_dir, row_id))
    thread = threading.Thread(
        target=_watch, args=(data_dir, client, row_id, op_id), name="op-watch", daemon=True
    )
    thread.start()


def track(db, data_dir: str, client, server_id: int, project: str, op_id: str, label: str) -> int:
    cursor = db.execute(
        "INSERT INTO tracked_operations (server_id, project, op_id, label) VALUES (?, ?, ?, ?)",
        (server_id, project, op_id, label),
    )
    db.commit()
    watch(data_dir, client, cursor.lastrowid, op_id)
    return cursor.lastrowid


def recent(db, server_id: int, project: str, limit: int = 20) -> list:
    return db.execute(
        "SELECT * FROM tracked_operations WHERE server_id = ? AND project = ? "
        "ORDER BY id DESC LIMIT ?",
        (server_id, project, limit),
    ).fetchall()


def resume(db, data_dir: str, client, server_id: int, project: str) -> None:
    """Watch again any still-running rows, e.g. after the console restarted."""
    for row in db.execute(
        "SELECT id, op_id FROM tracked_operations WHERE server_id = ? AND project = ? "
        "AND status = 'Running'",
        (server_id, project),
    ).fetchall():
        watch(data_dir, client, row["id"], row["op_id"])


def unseen(db, server_id: int, project: str) -> list:
    """Finished operations not yet reported, marked as reported."""
    rows = db.execute(
        "SELECT * FROM tracked_operations WHERE server_id = ? AND project = ? "
        "AND status != 'Running' AND seen = 0 ORDER BY id",
        (server_id, project),
    ).fetchall()
    if rows:
        db.executemany(
            "UPDATE tracked_operations SET seen = 1 WHERE id = ?", [(r["id"],) for r in rows]
        )
        db.commit()
    return rows
