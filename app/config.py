"""Default settings.

Override them in a Python file (``/etc/incus-console/config.py`` or the path in
``INCUS_CONSOLE_CONFIG``) or with ``INCUS_CONSOLE_<NAME>`` environment variables.
"""

# True: multi-user web app with a login screen (deb, rpm and container installs).
# False: single-user desktop app; the Electron runtime sets this and DESKTOP_KEY.
SERVER_MODE = True

# In desktop mode the runtime starts the server with a random key and opens
# ``/?key=<key>``. Requests without a session that presented the key get a 401.
DESKTOP_KEY = None

# Database, secret key and the client certificate live here.
DATA_DIR = "~/.incus-console"

SESSION_COOKIE_NAME = "incus_console_session"
SESSION_COOKIE_SAMESITE = "Lax"

# Seconds allowed for one Incus API call.
INCUS_TIMEOUT = 30

# Seconds a page waits for an Incus operation before handing off to Operations.
ACTION_WAIT = 30

DEFAULT_IMAGE_SERVER = "https://images.linuxcontainers.org"
IMAGE_SUGGESTIONS = ["debian/13", "debian/12", "ubuntu/26.04", "ubuntu/24.04", "almalinux/10"]

# What the browser terminal runs in a container: a login shell as root.
TERMINAL_COMMAND = ["su", "-l"]

# True behind nginx (or another proxy) serving HTTPS: trust its X-Forwarded-* headers
# and send the session cookie over HTTPS only.
BEHIND_PROXY = False

# Unix sockets offered on the Servers page when they exist and are accessible.
# Glob patterns are allowed; Colima keeps one socket per profile.
LOCAL_SOCKETS = [
    "/var/lib/incus/unix.socket",
    "/run/incus/unix.socket",
    "~/.colima/*/incus.sock",
]
