"""Images on the server: list, inspect, alias, delete, and publish from an instance."""

from __future__ import annotations

import re

from flask import flash, g, redirect, render_template, request, url_for

from .scoped import bp, run

ALIAS = re.compile(r"[^\s/][^\s]{0,254}")
ALIAS_RULE = "An alias has no spaces and does not start with a slash."


def _label(image: dict) -> str:
    return (image.get("properties") or {}).get("description") or image["fingerprint"][:12]


@bp.get("/images")
def images():
    rows = sorted(g.incus.images(), key=lambda i: _label(i).lower())
    return render_template("images/list.html", images=rows, label=_label)


@bp.get("/images/<fingerprint>")
def image(fingerprint):
    found = g.incus.image(fingerprint)
    return render_template("images/detail.html", image=found, label=_label(found))


@bp.post("/images/<fingerprint>/delete")
def image_delete(fingerprint):
    if run(lambda: g.incus.delete_image(fingerprint), "Image deleted."):
        return redirect(url_for("srv.images"))
    return redirect(url_for("srv.image", fingerprint=fingerprint))


@bp.post("/images/<fingerprint>/aliases")
def image_alias_create(fingerprint):
    alias = request.form.get("alias", "").strip()
    if not ALIAS.fullmatch(alias):
        flash(ALIAS_RULE, "error")
    else:
        description = request.form.get("description", "").strip()
        run(
            lambda: g.incus.add_image_alias(fingerprint, alias, description),
            f"Alias {alias} added.",
        )
    return redirect(url_for("srv.image", fingerprint=fingerprint))


@bp.post("/images/<fingerprint>/aliases/<path:alias>/delete")
def image_alias_delete(fingerprint, alias):
    run(lambda: g.incus.delete_image_alias(alias), f"Alias {alias} removed.")
    return redirect(url_for("srv.image", fingerprint=fingerprint))


@bp.route("/instances/<name>/publish", methods=["GET", "POST"])
def instance_publish(name):
    instance = g.incus.instance(name)
    form = request.form
    if request.method == "POST":
        alias = form.get("alias", "").strip()
        description = form.get("description", "").strip()
        if alias and not ALIAS.fullmatch(alias):
            flash(ALIAS_RULE, "error")
        elif run(
            lambda: g.incus.publish_instance(name, alias, description),
            f"Image of {name} published.",
        ):
            return redirect(url_for("srv.images"))
    image_description = (instance.get("config") or {}).get("image.description", "")
    default = f"{name} ({image_description})" if image_description else name
    return render_template(
        "instances/publish.html", instance=instance, form=form, default_description=default
    )
