from __future__ import annotations

from flask import current_app, g, redirect, render_template, url_for

from .. import tracker
from ..db import get_db
from .scoped import bp, run


@bp.get("/operations")
def operations():
    db = get_db()
    tracker.resume(db, current_app.config["DATA_DIR"], g.incus, g.sid, g.project)
    return render_template(
        "operations.html",
        operations=g.incus.operations(),
        tracked=tracker.recent(db, g.sid, g.project),
    )


@bp.post("/operations/<op_id>/cancel")
def operation_cancel(op_id):
    run(lambda: g.incus.cancel_operation(op_id), "Operation cancelled.")
    return redirect(url_for("srv.operations"))
