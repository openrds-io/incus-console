"""The settings form and the add-device form, shared by instances and profiles."""

from __future__ import annotations

import yaml

from ..incus import IncusError

# (key, label, kind, help). Empty means "not set here": the value comes from
# the profiles (for an instance), or Incus's default.
SETTINGS = [
    ("limits.cpu", "CPUs", "text", "A number of CPUs, or CPU ids such as 0-3."),
    ("limits.memory", "Memory", "text", "Such as 2GiB, or 50% of the host."),
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
]
DEVICE_TYPES = {
    "disk": "source=/srv/data\npath=/data",
    "proxy": "listen=tcp:0.0.0.0:8080\nconnect=tcp:127.0.0.1:80",
    "nic": "network=incusbr0\nname=eth1",
    "unix-char": "source=/dev/fuse\npath=/dev/fuse",
    "unix-block": "source=/dev/sdb\npath=/dev/sdb",
    "usb": "vendorid=0403\nproductid=6001",
    "gpu": "gputype=physical",
}


def apply_settings(config: dict, form) -> dict:
    """The config with the settings form's values; an empty field removes the key."""
    config = dict(config or {})
    for key, _label, kind, _help in SETTINGS:
        value = form.get(f"cfg:{key}", "").strip()
        if kind == "bool" and value not in ("", "true", "false"):
            value = ""
        if value:
            config[key] = value
        else:
            config.pop(key, None)
    return config


def settings_fields(local: dict, inherited: dict | None = None) -> list[dict]:
    local, inherited = local or {}, inherited or {}
    return [
        {
            "key": key,
            "label": label,
            "kind": kind,
            "help": help_text,
            "value": local.get(key, ""),
            "inherited": "" if key in local else inherited.get(key, ""),
        }
        for key, label, kind, help_text in SETTINGS
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
