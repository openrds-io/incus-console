from __future__ import annotations

from flask import g, redirect, render_template, url_for

from .scoped import bp, run


@bp.get("/operations")
def operations():
    return render_template("operations.html", operations=g.incus.operations())


@bp.post("/operations/<op_id>/cancel")
def operation_cancel(op_id):
    run(lambda: g.incus.cancel_operation(op_id), "Operation cancelled.")
    return redirect(url_for("srv.operations"))
