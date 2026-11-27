"""The settings form and the add-device form, shared by instances and profiles."""

from __future__ import annotations

import re

import yaml
from flask import current_app

from ..incus import IncusError

# (key, label, kind, help). Empty means "not set here": the value comes from
# the profiles (for an instance), or Incus's default.
SETTINGS = [
    ("limits.cpu", "CPUs", "text", "A number of CPUs, or CPU ids such as 0-3."),
    (
        "limits.memory",
        "Memory",
        "text",
        "Such as 2GiB, or 50% of the host. A plain number means GiB.",
    ),
    ("limits.processes", "Processes", "text", "The most processes it may run."),
    ("boot.autostart", "Start when the host starts", "bool", ""),
    (
        "security.nesting",
        "Allow nested containers",
        "bool",
        "Needed to run Docker or Incus inside.",
    ),
    (
        "security.privileged",
        "Privileged",
        "bool",
        "Root in the container is root on the host. Avoid unless you need it.",
    ),
    (
        "snapshots.schedule",
        "Snapshot schedule",
        "text",
        "@hourly, @daily, @weekly, @monthly, or cron syntax such as 0 3 * * * (03:00 daily).",
    ),
    (
        "snapshots.expiry",
        "Keep scheduled snapshots for",
        "text",
        "Such as 12H, 7d, 2w or 3m (hours, days, weeks, months). Empty keeps them.",
    ),
    ("snapshots.schedule.stopped", "Snapshot while stopped", "bool", ""),
]
# Only for application containers; Incus fills them in from the image.
OCI_SETTINGS = [
    (
        "oci.entrypoint",
        "Command",
        "text",
        "What the container runs, from the image. Change it only if that does not suit.",
    ),
    ("oci.cwd", "Working directory", "text", ""),
    ("oci.uid", "User id", "text", "Runs the command as this user id."),
    ("oci.gid", "Group id", "text", ""),
    (
        "boot.autorestart",
        "Restart if it exits",
        "bool",
        "Incus starts it again when its command ends or crashes (not when you stop it). "
        "Needs a recent Incus.",
    ),
]
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Headings shown above the first setting of each group.
GROUPS = {
    "oci.entrypoint": "Application",
    "limits.cpu": "Limits",
    "boot.autostart": "Behaviour",
    "snapshots.schedule": "Scheduled snapshots",
}
# What an empty field means, when "No limit" would be wrong.
PLACEHOLDERS = {
    "oci.entrypoint": "From the image",
    "oci.cwd": "From the image",
    "oci.uid": "From the image",
    "oci.gid": "From the image",
    "snapshots.schedule": "No scheduled snapshots",
    "snapshots.expiry": "Keep until deleted",
}
SCHEDULE_ALIASES = {
    "@hourly",
    "@daily",
    "@midnight",
    "@weekly",
    "@monthly",
    "@annually",
    "@yearly",
    "@startup",
    "@never",
}
CRON_FIELD = re.compile(r"[0-9*/,\-A-Za-z]+")
EXPIRY = re.compile(r"(\d+[MHdwmy])(\s+\d+[MHdwmy])*")


def check_schedule(value: str) -> None:
    """Raise IncusError unless value is an Incus snapshot schedule (comma-separated)."""
    for part in (p.strip() for p in value.split(",")):
        fields = part.split()
        if part in SCHEDULE_ALIASES or (
            len(fields) == 5 and all(CRON_FIELD.fullmatch(f) for f in fields)
        ):
            continue
        raise IncusError(400, f"{part!r} is not a schedule. Use @daily, or cron like 0 3 * * *.")


def check_expiry(value: str) -> None:
    if not EXPIRY.fullmatch(value):
        raise IncusError(400, f"{value!r} is not a duration. Use something like 7d or 2w.")


CHECKS = {"snapshots.schedule": check_schedule, "snapshots.expiry": check_expiry}
DEVICE_TYPES = {
    "disk": "source=/srv/data\npath=/data",
    "proxy": "listen=tcp:0.0.0.0:8080\nconnect=tcp:127.0.0.1:80",
    "nic": "network=incusbr0\nname=eth1",
    "unix-char": "source=/dev/fuse\npath=/dev/fuse",
    "unix-block": "source=/dev/sdb\npath=/dev/sdb",
    "usb": "vendorid=0403\nproductid=6001",
    "gpu": "gputype=physical",
}


def memory_size(text: str) -> str:
    """What someone typed for memory, as Incus wants it.

    Incus reads a number without a unit as bytes, so "2" would be a two-byte
    limit and an instance that cannot start. A whole number up to
    MEMORY_GIB_UP_TO is taken as GiB; anything else is passed on as it is.
    """
    text = text.strip()
    if text.isdecimal() and 0 < int(text) <= current_app.config["MEMORY_GIB_UP_TO"]:
        return f"{int(text)}GiB"
    return text


def apply_settings(
    config: dict,
    form,
    settings: list = SETTINGS,
    check: bool = True,
    choices: dict | None = None,
) -> dict:
    """The config with the settings form's values; an empty field removes the key.

    Kinds: text, bool (default/yes/no), choice (one of ``choices[key]``), and
    feature (a checkbox that is always written as true or false).
    Raises IncusError for a value Incus would refuse less clearly.
    """
    config = dict(config or {})
    for key, _label, kind, _help in settings:
        value = form.get(f"cfg:{key}", "").strip()
        if kind == "feature":
            config[key] = "true" if value else "false"
            continue
        if kind == "bool" and value not in ("", "true", "false"):
            value = ""
        if kind == "choice" and value not in (choices or {}).get(key, ()):
            value = ""
        if key == "limits.memory":
            value = memory_size(value)
        if check and value and key in CHECKS:
            CHECKS[key](value)
        if value:
            config[key] = value
        else:
            config.pop(key, None)
    return config


def settings_fields(
    local: dict,
    inherited: dict | None = None,
    settings: list = SETTINGS,
    empty: str = "No limit",
    *,
    groups: dict | None = None,
    choices: dict | None = None,
    defaults: dict | None = None,
) -> list[dict]:
    local, inherited = local or {}, inherited or {}
    if groups is None:
        groups = GROUPS if settings is SETTINGS else {}
    return [
        {
            "group": groups.get(key),
            "options": (choices or {}).get(key, ()),
            "default": (defaults or {}).get(key, ""),
            "empty": PLACEHOLDERS.get(key, empty),
            "key": key,
            "label": label,
            "kind": kind,
            "help": help_text,
            "value": local.get(key, ""),
            "inherited": "" if key in local else inherited.get(key, ""),
        }
        for key, label, kind, help_text in settings
    ]


def parse_settings(text: str) -> dict:
    settings = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise IncusError(400, f"Line {number}: write each setting as key=value.")
        settings[key.strip()] = value.strip()
    return settings


def new_device(form, owner: str, existing: dict) -> tuple[str, dict]:
    """The (name, device) the add-device form describes. Raises IncusError."""
    name = form.get("device", "").strip()
    kind = form.get("type", "")
    if not name:
        raise IncusError(400, "Give the device a name.")
    if kind not in DEVICE_TYPES:
        raise IncusError(400, "Choose a device type.")
    if name in (existing or {}):
        raise IncusError(400, f"{owner} already has a device named {name}.")
    return name, {**parse_settings(form.get("settings", "")), "type": kind}


def dump_yaml(obj: dict, keys: tuple) -> str:
    return yaml.safe_dump(
        {key: obj[key] for key in keys if key in obj}, sort_keys=False, default_flow_style=False
    )


def load_yaml(text: str, keys: tuple) -> dict:
    """The editable part of a YAML document. Raises IncusError."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise IncusError(400, f"Invalid YAML: {error}") from error
    if not isinstance(data, dict):
        raise IncusError(400, "The configuration must be a YAML mapping.")
    return {key: data[key] for key in keys if key in data}


def parse_environment(text: str) -> dict:
    """``NAME=value`` lines as Incus config: ``{"environment.NAME": "value"}``."""
    found = {}
    for name, value in parse_settings(text).items():
        if not ENV_NAME.fullmatch(name):
            raise IncusError(
                400, f"{name!r} is not a variable name. Use letters, digits and underscores."
            )
        found[f"environment.{name}"] = value
    return found


def environment_text(config: dict) -> str:
    """The environment.* keys of a config, one NAME=value per line."""
    prefix = "environment."
    return "\n".join(
        f"{key[len(prefix) :]}={value}"
        for key, value in sorted((config or {}).items())
        if key.startswith(prefix)
    )


def apply_environment(config: dict, text: str) -> dict:
    """Replace the config's environment.* keys with the form's. Raises IncusError."""
    kept = {k: v for k, v in (config or {}).items() if not k.startswith("environment.")}
    return {**kept, **parse_environment(text)}
