"""Keeping a cluster current: Incus upgrades on its members, and its certificate."""

from __future__ import annotations

import re

from flask import abort, flash, g, redirect, render_template, request, url_for

from .. import servers
from ..certs import check_pair, describe, new_pair
from ..incus import IncusError
from .cluster import _is_console_address, _versions
from .scoped import bp, cluster_members

# What to run on a member: Incus from the Zabbly repository, or Debian's own.
UPGRADE_COMMAND = "sudo apt update && sudo apt install --only-upgrade incus"


def version_key(version: str) -> tuple:
    """'7.10' sorts after '7.9'."""
    return tuple(int(part) if part.isdigit() else -1 for part in re.split(r"[.\-~+]", version))


@bp.get("/cluster/upgrade")
def cluster_upgrade():
    members = cluster_members()
    if not members:
        abort(404)
    versions = _versions(members)
    newest = max(versions.values(), key=version_key, default="")
    rows = []
    for member in members:
        name, status = member["server_name"], member.get("status") or ""
        version = versions.get(name, "")
        if status == "Blocked":
            state = "waiting"
        elif not version:
            state = "unknown"
        elif version == newest:
            state = "current"
        else:
            state = "behind"
        rows.append(
            {
                "name": name,
                "version": version,
                "status": status,
                "message": member.get("message") or "",
                "state": state,
                "console": _is_console_address(member),
            }
        )
    # The member the console reaches the cluster through goes last.
    rows.sort(key=lambda row: (row["console"], row["name"]))
    return render_template(
        "cluster/upgrade.html",
        rows=rows,
        newest=newest,
        done=all(row["state"] == "current" for row in rows),
        command=UPGRADE_COMMAND,
    )


def _current_certificate() -> dict:
    try:
        info = g.incus.server_info()
    except IncusError:
        return {}
    return describe((info.get("environment") or {}).get("certificate") or "")


def _follow(fingerprint: str) -> int:
    """Pin the new certificate for every registration that reaches a member; how many."""
    addresses = {(m.get("url") or "").rstrip("/") for m in cluster_members()}
    moved = 0
    for row in servers.all_servers():
        if row["kind"] == "https" and row["address"].rstrip("/") in addresses:
            servers.set_fingerprint(row["id"], fingerprint)
            moved += 1
    return moved


@bp.route("/cluster/certificate", methods=["GET", "POST"])
def cluster_certificate():
    if not cluster_members():
        abort(404)
    if request.method == "POST":
        if request.form.get("source") == "paste":
            certificate = request.form.get("certificate", "").strip() + "\n"
            key = request.form.get("key", "").strip() + "\n"
            problem = check_pair(certificate, key)
        else:
            certificate, key = new_pair("incus-cluster")
            problem = None
        if problem:
            flash(problem, "error")
            return redirect(url_for("srv.cluster_certificate"))
        try:
            g.incus.update_cluster_certificate(certificate, key)
        except IncusError as error:
            flash(error.message, "error")
            return redirect(url_for("srv.cluster_certificate"))
        _follow(describe(certificate)["fingerprint"])
        flash("Every member now uses the new cluster certificate.", "success")
        return redirect(url_for("srv.cluster_certificate"))
    return render_template("cluster/certificate.html", current=_current_certificate())
