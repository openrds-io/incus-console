from __future__ import annotations

from flask import flash, g, redirect, render_template, request, url_for

from ..incus import IncusError
from .scoped import bp, run


@bp.get("/storage")
def pools():
    return render_template("storage/pools.html", pools=g.incus.pools())


@bp.route("/storage/<pool>/buckets", methods=["GET", "POST"])
def buckets(pool):
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        size = request.form.get("size", "").strip()
        payload = {
            "name": name,
            "description": request.form.get("description", "").strip(),
            "config": {"size": size} if size else {},
        }
        if not name:
            flash("Give the bucket a name.", "error")
        elif run(lambda: g.incus.create_bucket(pool, payload), f"Bucket {name} created."):
            return redirect(url_for("srv.bucket", pool=pool, bucket=name))
        return redirect(url_for("srv.buckets", pool=pool))

    rows, problem = [], None
    try:
        rows = g.incus.buckets(pool)
    except IncusError as error:
        if error.status == 0:
            raise
        problem = error.message
    # Only administrators can read the server config; None means "unknown".
    endpoint = (g.incus.server_info().get("config") or {}).get("core.storage_buckets_address")
    return render_template(
        "storage/buckets.html", pool=pool, buckets=rows, problem=problem, endpoint=endpoint
    )


@bp.get("/storage/<pool>/buckets/<bucket>")
def bucket(pool, bucket):
    return render_template(
        "storage/bucket.html",
        pool=pool,
        bucket=g.incus.bucket(pool, bucket),
        keys=g.incus.bucket_keys(pool, bucket),
    )


@bp.post("/storage/<pool>/buckets/<bucket>/delete")
def bucket_delete(pool, bucket):
    if run(lambda: g.incus.delete_bucket(pool, bucket), f"Bucket {bucket} deleted."):
        return redirect(url_for("srv.buckets", pool=pool))
    return redirect(url_for("srv.bucket", pool=pool, bucket=bucket))


@bp.post("/storage/<pool>/buckets/<bucket>/keys")
def bucket_key_create(pool, bucket):
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "read-only")
    if not name:
        flash("Give the key a name.", "error")
    elif role not in ("admin", "read-only"):
        flash("Unknown role.", "error")
    else:
        payload = {"name": name, "role": role}
        run(lambda: g.incus.create_bucket_key(pool, bucket, payload), f"Key {name} created.")
    return redirect(url_for("srv.bucket", pool=pool, bucket=bucket))


@bp.post("/storage/<pool>/buckets/<bucket>/keys/<key>/delete")
def bucket_key_delete(pool, bucket, key):
    run(lambda: g.incus.delete_bucket_key(pool, bucket, key), f"Key {key} deleted.")
    return redirect(url_for("srv.bucket", pool=pool, bucket=bucket))
