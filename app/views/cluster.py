"""Clusters: the members of the cluster a server belongs to, evacuate and restore."""

from __future__ import annotations

from flask import abort, flash, g, redirect, render_template, request, url_for

from .scoped import bp, cluster_members, run

# How an evacuation treats the member's instances. Containers cannot move while
# running, so "migrate" stops each one, moves it and starts it again.
EVACUATE_MODES = {
    "": "As each instance is set (cluster.evacuate)",
    "migrate": "Move them to other members",
    "stop": "Stop them and leave them here",
}


@bp.get("/cluster")
def cluster():
    members = cluster_members()
    counts: dict[str, int] = {}
    if members:
        for instance in g.incus.instances():
            location = instance.get("location") or ""
            counts[location] = counts.get(location, 0) + 1
    return render_template(
        "cluster/members.html", members=members, counts=counts, modes=EVACUATE_MODES
    )


def _member(name: str) -> dict:
    found = next((m for m in cluster_members() if m["server_name"] == name), None)
    if found is None:
        abort(404)
    return found


@bp.post("/cluster/members/<member>/evacuate")
def cluster_evacuate(member):
    _member(member)
    mode = request.form.get("mode", "")
    if mode not in EVACUATE_MODES:
        flash("Choose what happens to the instances.", "error")
    else:
        run(
            lambda: g.incus.cluster_member_state(member, "evacuate", mode),
            f"{member} evacuated.",
        )
    return redirect(url_for("srv.cluster"))


@bp.post("/cluster/members/<member>/restore")
def cluster_restore(member):
    _member(member)
    run(lambda: g.incus.cluster_member_state(member, "restore"), f"{member} restored.")
    return redirect(url_for("srv.cluster"))
