"""Networks: managed bridges, their DHCP leases, and port forwards."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlsplit

from flask import flash, g, redirect, render_template, request, url_for

from ..incus import IncusError
from .config_forms import apply_settings, dump_yaml, load_yaml, settings_fields
from .scoped import bp, run

EDITABLE = ("config", "description")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
NAME_RULE = "Use letters, digits, dashes, dots and underscores (at most 15, a Linux limit)."
SETTINGS = [
    (
        "ipv4.address",
        "IPv4 address",
        "text",
        "The bridge's address and subnet, such as 10.10.10.1/24; auto picks a free one; none.",
    ),
    ("ipv4.nat", "IPv4 NAT", "bool", "Lets containers reach outside networks through the host."),
    ("ipv6.address", "IPv6 address", "text", "Such as fd42::1/64; auto; or none."),
    ("ipv6.nat", "IPv6 NAT", "bool", ""),
    ("dns.domain", "DNS domain", "text", "Containers are reachable as <name>.<domain>."),
]
PORTS = re.compile(r"\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*")
PORT_ROWS = 5


def used_by(urls: list | None) -> dict[str, list[str]]:
    """Instances and profiles in a used_by list, by name."""
    found: dict[str, list[str]] = {"instances": [], "profiles": []}
    for url in urls or []:
        parts = urlsplit(url).path.split("/")
        if len(parts) == 4 and parts[2] in found:
            found[parts[2]].append(unquote(parts[3]))
    return {kind: sorted(names) for kind, names in found.items()}


def _crumbs(name: str, *more) -> list:
    items = [("Networks", url_for("srv.networks"))]
    if not more:
        return [*items, (name, None)]
    return [*items, (name, url_for("srv.network", name=name)), *more]


def _managed(name: str) -> dict:
    found = g.incus.network(name)
    if not found.get("managed"):
        raise IncusError(400, f"{name} is not managed by Incus, so it cannot be changed here.")
    return found


@bp.get("/networks")
def networks():
    rows = sorted(g.incus.networks(), key=lambda n: (not n.get("managed"), n["name"]))
    for row in rows:
        row["users"] = used_by(row.get("used_by"))
    return render_template("networks/list.html", networks=rows)


@bp.route("/networks/new", methods=["GET", "POST"])
def network_create():
    form = request.form
    defaults = {"ipv4.address": "auto", "ipv4.nat": "true", "ipv6.address": "auto"}
    if request.method == "POST":
        name = form.get("name", "").strip()
        payload = {
            "name": name,
            "type": "bridge",
            "description": form.get("description", "").strip(),
            "config": apply_settings({}, form, SETTINGS),
        }
        if not NAME.fullmatch(name):
            flash(NAME_RULE, "error")
        elif run(lambda: g.incus.create_network(payload), f"Network {name} created."):
            return redirect(url_for("srv.network", name=name))
        defaults = payload["config"]
    return render_template(
        "networks/new.html",
        form=form,
        fields=settings_fields(defaults, settings=SETTINGS, empty="Default"),
    )


@bp.get("/networks/<name>")
def network(name):
    found = g.incus.network(name)
    leases, forwards, state = [], [], {}
    if found.get("managed"):
        leases = g.incus.network_leases(name)
        forwards = g.incus.network_forwards(name)
    try:
        state = g.incus.network_state(name)
    except IncusError as error:
        if error.status == 0:
            raise
    return render_template(
        "networks/detail.html",
        network=found,
        users=used_by(found.get("used_by")),
        leases=sorted(leases, key=lambda lease: lease.get("hostname") or ""),
        forwards=forwards,
        state=state,
    )


@bp.route("/networks/<name>/settings", methods=["GET", "POST"])
def network_settings(name):
    found = _managed(name)
    if request.method == "POST":
        payload = {key: found[key] for key in EDITABLE if key in found}
        payload["config"] = apply_settings(payload.get("config"), request.form, SETTINGS)
        payload["description"] = request.form.get("description", "").strip()
        if run(lambda: g.incus.update_network(name, payload), f"Network {name} updated."):
            return redirect(url_for("srv.network", name=name))
        found = {**found, **payload}
    return render_template(
        "config/settings.html",
        title=f"Settings - {name}",
        crumbs=_crumbs(name, ("Settings", None)),
        back=url_for("srv.network", name=name),
        yaml_href=url_for("srv.network_edit", name=name),
        description=found.get("description") or "",
        fields=settings_fields(found.get("config"), settings=SETTINGS, empty="Default"),
        intro="Leave a field empty to use Incus's default. Changing addresses "
        "briefly interrupts the containers on this network.",
    )


@bp.route("/networks/<name>/edit", methods=["GET", "POST"])
def network_edit(name):
    if request.method == "POST":
        text = request.form.get("yaml", "")
        try:
            payload = load_yaml(text, EDITABLE)
        except IncusError as error:
            flash(error.message, "error")
        else:
            if run(lambda: g.incus.update_network(name, payload), f"Network {name} updated."):
                return redirect(url_for("srv.network", name=name))
    else:
        text = dump_yaml(_managed(name), EDITABLE)
    return render_template(
        "config/yaml.html",
        title=f"Edit {name}",
        crumbs=_crumbs(name, ("Edit YAML", None)),
        back=url_for("srv.network", name=name),
        command=f"incus network edit {name}",
        volatile=True,
        text=text,
    )


@bp.route("/networks/<name>/rename", methods=["GET", "POST"])
def network_rename(name):
    _managed(name)
    if request.method == "POST":
        new_name = request.form.get("new_name", "").strip()
        if not NAME.fullmatch(new_name):
            flash(NAME_RULE, "error")
        elif new_name == name:
            return redirect(url_for("srv.network", name=name))
        elif run(lambda: g.incus.rename_network(name, new_name), f"{name} renamed to {new_name}."):
            return redirect(url_for("srv.network", name=new_name))
    return render_template("networks/rename.html", name=name, form=request.form)


@bp.post("/networks/<name>/delete")
def network_delete(name):
    if run(lambda: g.incus.delete_network(name), f"Network {name} deleted."):
        return redirect(url_for("srv.networks"))
    return redirect(url_for("srv.network", name=name))


# -- port forwards ----------------------------------------------------------


def _address(value: str, what: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as error:
        raise IncusError(400, f"{what} must be an IP address, not {value!r}.") from error


def _forward_form(form) -> dict:
    """The forward the form describes. Raises IncusError."""
    target = form.get("target_address", "").strip()
    forward = {
        "description": form.get("description", "").strip(),
        "config": {"target_address": _address(target, "The default target")} if target else {},
        "ports": [],
    }
    for row in range(PORT_ROWS):
        listen_port = form.get(f"listen_port_{row}", "").strip()
        if not listen_port:
            continue
        protocol = form.get(f"protocol_{row}", "tcp")
        target_port = form.get(f"target_port_{row}", "").strip()
        row_target = form.get(f"target_address_{row}", "").strip()
        if protocol not in ("tcp", "udp"):
            raise IncusError(400, "Choose TCP or UDP.")
        for ports in filter(None, (listen_port, target_port)):
            if not PORTS.fullmatch(ports):
                raise IncusError(400, f"{ports!r} is not a port, range (8000-8010) or list.")
        if not row_target and not target:
            raise IncusError(400, f"Port {listen_port} needs a target address.")
        port = {
            "protocol": protocol,
            "listen_port": listen_port,
            "target_address": _address(row_target, "A target") if row_target else target,
            "description": form.get(f"port_description_{row}", "").strip(),
        }
        if target_port:
            port["target_port"] = target_port
        forward["ports"].append(port)
    return forward


def _instance_addresses(name: str) -> list[str]:
    """Addresses handed out on this network, offered as targets."""
    leases = g.incus.network_leases(name)
    return sorted({lease["address"] for lease in leases if lease.get("address")})


def _render_forward(name, form, *, listen=None, forward=None):
    rows = []
    ports = (forward or {}).get("ports") or []
    for row in range(PORT_ROWS):
        port = ports[row] if row < len(ports) and not form else {}
        rows.append(
            {
                "protocol": form.get(f"protocol_{row}") or port.get("protocol", "tcp"),
                "listen_port": form.get(f"listen_port_{row}", port.get("listen_port", "")),
                "target_address": form.get(
                    f"target_address_{row}",
                    ""
                    if port.get("target_address")
                    == ((forward or {}).get("config") or {}).get("target_address")
                    else port.get("target_address", ""),
                ),
                "target_port": form.get(f"target_port_{row}", port.get("target_port", "")),
                "description": form.get(f"port_description_{row}", port.get("description", "")),
            }
        )
    return render_template(
        "networks/forward.html",
        name=name,
        listen=listen,
        forward=forward or {},
        form=form,
        rows=rows,
        addresses=_instance_addresses(name),
        crumbs=_crumbs(name, (f"Forward {listen}" if listen else "Add port forward", None)),
    )


@bp.route("/networks/<name>/forwards/new", methods=["GET", "POST"])
def forward_create(name):
    _managed(name)
    form = request.form
    if request.method == "POST":
        try:
            listen = _address(form.get("listen_address", "").strip(), "The listen address")
            payload = {"listen_address": listen, **_forward_form(form)}
        except IncusError as error:
            flash(error.message, "error")
        else:
            done = f"Forward on {listen} added."
            if run(lambda: g.incus.create_network_forward(name, payload), done):
                return redirect(url_for("srv.network", name=name))
    return _render_forward(name, form)


@bp.route("/networks/<name>/forwards/<listen>", methods=["GET", "POST"])
def forward_edit(name, listen):
    forward = g.incus.network_forward(name, listen)
    form = request.form
    if request.method == "POST":
        try:
            payload = _forward_form(form)
        except IncusError as error:
            flash(error.message, "error")
        else:
            done = f"Forward on {listen} updated."
            if run(lambda: g.incus.update_network_forward(name, listen, payload), done):
                return redirect(url_for("srv.network", name=name))
    return _render_forward(name, form, listen=listen, forward=forward)


@bp.post("/networks/<name>/forwards/<listen>/delete")
def forward_delete(name, listen):
    run(lambda: g.incus.delete_network_forward(name, listen), f"Forward on {listen} removed.")
    return redirect(url_for("srv.network", name=name))
