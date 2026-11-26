"""Storage pools and object storage: buckets and their access keys."""

from __future__ import annotations

import re

from flask import flash, g, make_response, redirect, render_template, request, url_for

from ..incus import IncusError
from .scoped import bp, cluster_members, run

# Incus follows S3's rules for bucket names.
BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
BUCKET_RULE = (
    "Use 3 to 63 lowercase letters, digits, dots and dashes, "
    "starting and ending with a letter or digit."
)
ROLES = {"read-only": "Read-only", "admin": "Admin"}


# Drivers the create form offers, where the server supports them. The others
# (ceph, cephfs, linstor, ...) need settings a form like this cannot guess.
DRIVERS = {
    "dir": "Directory (dir)",
    "zfs": "ZFS",
    "btrfs": "Btrfs",
    "lvm": "LVM",
}
POOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
POOL_RULE = "Use letters, digits, dashes, dots and underscores (at most 63)."
POOL_SIZE = re.compile(r"\d+(\.\d+)?\s*([KMGTPE]i?B)", re.IGNORECASE)


@bp.get("/storage")
def pools():
    return render_template("storage/pools.html", pools=g.incus.pools())


def _drivers() -> dict[str, str]:
    """The drivers this server can use, of those the form knows."""
    environment = g.incus.server_info().get("environment") or {}
    supported = {
        str(d.get("Name") or d.get("name") or "").lower()
        for d in environment.get("storage_supported_drivers") or []
    }
    offered = {name: label for name, label in DRIVERS.items() if name in supported}
    # A server that does not say (or says it another way) gets the whole list.
    return offered or dict(DRIVERS)


def _create_pool(payload: dict, sources: dict[str, str]) -> dict:
    """Create a pool; in a cluster, define it on every member first.

    Where the data lives (source) and how big a new one is (size) belong to
    each member, so in a cluster they go with the member's own step.
    """
    members = [m["server_name"] for m in cluster_members()]
    if not members:
        return g.incus.create_pool(payload)
    local = {k: v for k, v in payload["config"].items() if k == "size"}
    for member in members:
        config = dict(local)
        if sources.get(member):
            config["source"] = sources[member]
        pending = {"name": payload["name"], "driver": payload["driver"], "config": config}
        try:
            g.incus.create_pool(pending, target=member)
        except IncusError as error:
            # Left over from an earlier attempt that did not get to the last step.
            if "already" not in error.message.lower():
                raise
    final = {**payload, "config": {}}
    return g.incus.create_pool(final)


@bp.route("/storage/new", methods=["GET", "POST"])
def pool_create():
    drivers = _drivers()
    members = [m["server_name"] for m in cluster_members()]
    form = request.form
    if request.method == "POST":
        name = form.get("name", "").strip()
        driver = form.get("driver", "")
        size = form.get("size", "").strip().replace(" ", "")
        sources = {m: form.get(f"source:{m}", "").strip() for m in members}
        config = {}
        if size:
            config["size"] = size
        if not members and form.get("source", "").strip():
            config["source"] = form.get("source", "").strip()
        payload = {
            "name": name,
            "driver": driver,
            "description": form.get("description", "").strip(),
            "config": config,
        }
        if not POOL_NAME.fullmatch(name):
            flash(POOL_RULE, "error")
        elif driver not in drivers:
            flash("Choose a storage driver.", "error")
        elif size and not POOL_SIZE.fullmatch(size):
            flash("Write a size such as 20GiB.", "error")
        elif size and driver == "dir":
            flash("A directory pool has no size of its own: leave the size empty.", "error")
        elif run(lambda: _create_pool(payload, sources), f"Storage pool {name} created."):
            return redirect(url_for("srv.pool", pool=name))
    return render_template("storage/pool_new.html", drivers=drivers, members=members, form=form)


def _usage(pool: str, member: str | None = None) -> dict:
    """{"used", "total"} in bytes, or {} where the pool cannot say."""
    try:
        return (g.incus.pool_resources(pool, target=member).get("space")) or {}
    except IncusError as error:
        if error.status == 0:
            raise
        return {}


@bp.get("/storage/<pool>")
def pool(pool):
    found = g.incus.pool(pool)
    per_member = []
    for member in cluster_members():
        name = member["server_name"]
        row = {"name": name, "status": member.get("status"), "source": "", "usage": {}}
        if member.get("status") == "Online":
            try:
                row["source"] = (g.incus.pool(pool, target=name).get("config") or {}).get(
                    "source", ""
                )
            except IncusError as error:
                if error.status == 0:
                    raise
            row["usage"] = _usage(pool, name)
        per_member.append(row)
    return render_template(
        "storage/pool.html",
        pool=found,
        usage={} if per_member else _usage(pool),
        per_member=per_member,
        in_use=len(found.get("used_by") or []),
    )


@bp.post("/storage/<pool>/delete")
def pool_delete(pool):
    if run(lambda: g.incus.delete_pool(pool), f"Storage pool {pool} deleted."):
        return redirect(url_for("srv.pools"))
    return redirect(url_for("srv.pool", pool=pool))


def _endpoint():
    """The server's S3 address: a string, "" when unset, None when we may not read it."""
    return (g.incus.server_info().get("config") or {}).get("core.storage_buckets_address")


def _with_key_counts(pool: str, rows: list) -> list:
    for bucket in rows:
        bucket["pool"] = pool
        try:
            bucket["key_count"] = len(g.incus.bucket_keys(pool, bucket["name"]))
        except IncusError as error:
            if error.status == 0:
                raise
            bucket["key_count"] = None
    return rows


@bp.get("/buckets")
def all_buckets():
    """Every bucket in the project, across all pools."""
    rows, problems = [], []
    for pool in g.incus.pools():
        try:
            rows += _with_key_counts(pool["name"], g.incus.buckets(pool["name"]))
        except IncusError as error:
            if error.status == 0:
                raise
            problems.append(f"{pool['name']}: {error.message}")
    rows.sort(key=lambda b: (b["name"], b["pool"]))
    return render_template(
        "storage/buckets.html",
        pool=None,
        buckets=rows,
        problems=problems,
        endpoint=_endpoint(),
    )


@bp.get("/storage/<pool>/buckets")
def buckets(pool):
    rows, problems = [], []
    try:
        rows = _with_key_counts(pool, g.incus.buckets(pool))
    except IncusError as error:
        if error.status == 0:
            raise
        problems.append(error.message)
    return render_template(
        "storage/buckets.html",
        pool=pool,
        buckets=rows,
        problems=problems,
        endpoint=_endpoint(),
    )


def _bucket_form(form) -> tuple[str, dict]:
    """Validate the create-bucket form; return (pool, payload)."""
    name = form.get("name", "").strip()
    pool = form.get("pool", "").strip()
    size = form.get("size", "").strip()
    if not pool:
        raise IncusError(400, "Choose a storage pool.")
    if not BUCKET_NAME.fullmatch(name) or ".." in name:
        raise IncusError(400, BUCKET_RULE)
    payload = {
        "name": name,
        "description": form.get("description", "").strip(),
        "config": {"size": size} if size else {},
    }
    return pool, payload


@bp.route("/buckets/new", methods=["GET", "POST"])
def bucket_create():
    pools = [p["name"] for p in g.incus.pools()]
    form = request.form if request.method == "POST" else request.args
    if request.method == "POST":
        try:
            pool, payload = _bucket_form(form)
            admin_key = g.incus.create_bucket(pool, payload)
        except IncusError as error:
            if error.status == 0:
                raise
            flash(error.message, "error")
        else:
            bucket = g.incus.bucket(pool, payload["name"])
            # Shown once: the page is not cached and the key is not kept anywhere else.
            return _no_store(
                render_template(
                    "storage/credentials.html",
                    pool=pool,
                    bucket=bucket,
                    key=admin_key,
                    created="bucket",
                )
            )
    return render_template("storage/bucket_form.html", pools=pools, form=form, bucket=None)


@bp.get("/storage/<pool>/buckets/<bucket>")
def bucket(pool, bucket):
    return render_template(
        "storage/bucket.html",
        pool=pool,
        bucket=g.incus.bucket(pool, bucket),
        keys=g.incus.bucket_keys(pool, bucket),
        roles=ROLES,
    )


@bp.route("/storage/<pool>/buckets/<bucket>/edit", methods=["GET", "POST"])
def bucket_edit(pool, bucket):
    current = g.incus.bucket(pool, bucket)
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        config = dict(current.get("config") or {})
        size = form.get("size", "").strip()
        if size:
            config["size"] = size
        else:
            config.pop("size", None)
        payload = {"description": form.get("description", "").strip(), "config": config}
        if run(lambda: g.incus.update_bucket(pool, bucket, payload), f"Bucket {bucket} updated."):
            return redirect(url_for("srv.bucket", pool=pool, bucket=bucket))
    return render_template(
        "storage/bucket_form.html", pools=[pool], form=form, bucket=current, pool=pool
    )


@bp.post("/storage/<pool>/buckets/<bucket>/delete")
def bucket_delete(pool, bucket):
    if run(lambda: g.incus.delete_bucket(pool, bucket), f"Bucket {bucket} deleted."):
        return redirect(url_for("srv.all_buckets"))
    return redirect(url_for("srv.bucket", pool=pool, bucket=bucket))


# -- keys -----------------------------------------------------------------


def _key_form(form, *, editing: dict | None = None) -> dict:
    role = form.get("role", "read-only")
    if role not in ROLES:
        raise IncusError(400, "Unknown role.")
    payload = {"role": role, "description": form.get("description", "").strip()}
    access = form.get("access_key", "").strip()
    secret = form.get("secret_key", "").strip()
    if editing is not None:
        # Empty means "keep": Incus would otherwise make new ones.
        access = access or editing.get("access-key", "")
        secret = secret or editing.get("secret-key", "")
    if access:
        payload["access-key"] = access
    if secret:
        payload["secret-key"] = secret
    return payload


@bp.route("/storage/<pool>/buckets/<bucket>/keys", methods=["GET", "POST"])
def bucket_key_create(pool, bucket):
    current = g.incus.bucket(pool, bucket)
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        name = form.get("name", "").strip()
        try:
            if not name:
                raise IncusError(400, "Give the key a name.")
            payload = {"name": name, **_key_form(form)}
            created = g.incus.create_bucket_key(pool, bucket, payload)
        except IncusError as error:
            if error.status == 0:
                raise
            flash(error.message, "error")
        else:
            return _no_store(
                render_template(
                    "storage/credentials.html",
                    pool=pool,
                    bucket=current,
                    key=created,
                    created="key",
                )
            )
    return render_template(
        "storage/key_form.html", pool=pool, bucket=current, key=None, form=form, roles=ROLES
    )


@bp.route("/storage/<pool>/buckets/<bucket>/keys/<key>/edit", methods=["GET", "POST"])
def bucket_key_edit(pool, bucket, key):
    current = g.incus.bucket(pool, bucket)
    existing = g.incus.bucket_key(pool, bucket, key)
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        try:
            payload = _key_form(form, editing=existing)
        except IncusError as error:
            flash(error.message, "error")
        else:
            if run(
                lambda: g.incus.update_bucket_key(pool, bucket, key, payload),
                f"Key {key} updated.",
            ):
                return redirect(url_for("srv.bucket", pool=pool, bucket=bucket))
    return render_template(
        "storage/key_form.html", pool=pool, bucket=current, key=existing, form=form, roles=ROLES
    )


@bp.post("/storage/<pool>/buckets/<bucket>/keys/<key>/delete")
def bucket_key_delete(pool, bucket, key):
    run(lambda: g.incus.delete_bucket_key(pool, bucket, key), f"Key {key} deleted.")
    return redirect(url_for("srv.bucket", pool=pool, bucket=bucket))


def _no_store(body: str):
    response = make_response(body)
    response.headers["Cache-Control"] = "no-store"
    return response
