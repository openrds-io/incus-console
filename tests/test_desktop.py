"""The desktop server entry point, run the way the Electron runtime runs it."""

import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def start(env):
    return subprocess.Popen(
        [sys.executable, "-m", "app.desktop"],
        cwd=ROOT,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_desktop_server(tmp_path):
    key = secrets.token_urlsafe(32)
    proc = start({"INCUS_CONSOLE_DESKTOP_KEY": key, "INCUS_CONSOLE_DATA_DIR": str(tmp_path)})
    try:
        line = proc.stdout.readline()
        assert line.startswith("LISTENING "), proc.stderr.read()
        base = f"http://127.0.0.1:{int(line.split()[1])}"
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{base}/servers")
        assert refused.value.code == 401
        with urllib.request.urlopen(f"{base}/servers?key={key}") as response:
            assert response.status == 200
            assert "No servers yet" in response.read().decode()
        assert (tmp_path / "incus-console.db").exists()
    finally:
        proc.terminate()
        proc.wait(5)


def test_desktop_server_needs_a_key(tmp_path):
    proc = start({"INCUS_CONSOLE_DESKTOP_KEY": "short", "INCUS_CONSOLE_DATA_DIR": str(tmp_path)})
    _out, err = proc.communicate(timeout=10)
    assert proc.returncode != 0
    assert "INCUS_CONSOLE_DESKTOP_KEY" in err
