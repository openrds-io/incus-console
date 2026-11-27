"""The console's version: Ubuntu style, YY.MM.N.

YY.MM is the year and month of the release series; N counts releases within it
(26.11.1, 26.11.2, ...). A new feature release takes the month it ships in.
Releases are tagged v<version>. desktop/package.json carries the same number.

Builds made from anything other than the release tag show "-dev" and the commit.
Packaging writes the commit to app/COMMIT, since installs have no git checkout.
"""

from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path

__version__ = "26.11.10"

HERE = Path(__file__).resolve().parent


def _git(*args: str) -> str | None:
    try:
        # Fixed arguments; only used to label development checkouts.
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(HERE.parent), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


@functools.cache
def build() -> dict:
    """{"version", "commit", "release", "label"} for display."""
    commit = os.environ.get("INCUS_CONSOLE_COMMIT")
    stamp = HERE / "COMMIT"
    release = False
    if not commit and stamp.exists():
        # Written by packaging: "<commit>" or "<commit> release".
        parts = stamp.read_text().split()
        commit = parts[0] if parts else None
        release = "release" in parts[1:]
    elif not commit:
        commit = _git("rev-parse", "--short=12", "HEAD")
        release = (
            commit is not None
            and _git("describe", "--tags", "--exact-match") == f"v{__version__}"
            and not _git("status", "--porcelain", "--untracked-files=no")
        )
    label = __version__ if release else f"{__version__}-dev"
    return {"version": __version__, "commit": commit, "release": release, "label": label}
