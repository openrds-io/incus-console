"""The Prometheus text format, as Incus's /1.0/metrics answers."""

from __future__ import annotations

import re
from collections.abc import Iterator

LINE = re.compile(r"([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+-?\d+)?")
LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def _unescape(value: str) -> str:
    return value.replace(r"\"", '"').replace(r"\n", "\n").replace("\\\\", "\\")


def parse(text: str) -> Iterator[tuple[str, dict[str, str], float]]:
    """(name, labels, value) for each sample; comments and odd lines are skipped."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        found = LINE.fullmatch(line)
        if not found:
            continue
        name, labels, value = found.groups()
        try:
            number = float(value)
        except ValueError:
            continue
        yield name, {k: _unescape(v) for k, v in LABEL.findall(labels or "")}, number
