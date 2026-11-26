"""Default settings.

Override them in a Python file (``/etc/incus-console/config.py`` or the path in
``INCUS_CONSOLE_CONFIG``) or with ``INCUS_CONSOLE_<NAME>`` environment variables.
"""

# True: multi-user web app with a login screen (deb, rpm and container installs).
# False: single-user desktop app; the Electron runtime sets this and DESKTOP_KEY.
SERVER_MODE = True

# In desktop mode the runtime starts the server (app/desktop.py) with a random key
# for that launch and opens ``/?key=<key>``. Requests without a session that
# presented this launch's key get a 401.
DESKTOP_KEY = None

# Database, secret key and the client certificate live here.
DATA_DIR = "~/.incus-console"

SESSION_COOKIE_NAME = "incus_console_session"
SESSION_COOKIE_SAMESITE = "Lax"

# Largest request body, i.e. the most one file-browser upload may send (bytes).
# Behind nginx, raise client_max_body_size to match.
MAX_CONTENT_LENGTH = 1024 * 1024 * 1024

# Seconds allowed for one Incus API call.
INCUS_TIMEOUT = 30

# Seconds a page waits for an Incus operation before handing off to Operations.
ACTION_WAIT = 30

# Memory fields: a whole number up to this, typed without a unit, means GiB.
MEMORY_GIB_UP_TO = 512

DEFAULT_IMAGE_SERVER = "https://images.linuxcontainers.org"

# Offer the default image server's catalog on the create form. The console
# fetches the server's index (several MB) and keeps it for an hour.
IMAGE_CATALOG = True
IMAGE_SUGGESTIONS = ["debian/13", "debian/12", "ubuntu/26.04", "ubuntu/24.04", "almalinux/10"]

# OCI application containers (Incus 6.3 and later, with skopeo and umoci on the
# Incus server): image names without a registry come from this one. The server
# pulls the image itself; the console never contacts the registry.
OCI_REGISTRY = "https://docker.io"
OCI_SUGGESTIONS = [
    "nginx:alpine",
    "caddy:alpine",
    "postgres:18-alpine",
    "redis:alpine",
    "ghcr.io/home-assistant/home-assistant:stable",
]

# What "Use the usual settings" fills in on the create form, by image name
# (the last part of the reference, without the tag): environment variables,
# published ports (host:container) and volumes (name:/path in the container).
OCI_PRESETS = {
    "nginx": {"ports": ["8080:80"]},
    "caddy": {"ports": ["8080:80", "8443:443"], "volumes": ["caddy-data:/data"]},
    "postgres": {
        "environment": ["POSTGRES_PASSWORD=change-me"],
        "ports": ["5432:5432"],
        "volumes": ["pgdata:/var/lib/postgresql"],
    },
    "redis": {"ports": ["6379:6379"], "volumes": ["redis-data:/data"]},
    "home-assistant": {
        "environment": ["TZ=America/New_York"],
        "ports": ["8123:8123"],
        "volumes": ["ha-config:/config"],
    },
}

# What the browser terminal runs in a container: a login shell as root.
TERMINAL_COMMAND = ["su", "-l"]
# Application containers rarely have su or a login setup; most have sh.
TERMINAL_COMMAND_OCI = ["sh"]

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

# Server mode: days of audit log to keep (0 keeps everything).
AUDIT_KEEP_DAYS = 365

# Server mode: after this many failed sign-ins for one username within
# LOGIN_LOCKOUT_SECONDS, further attempts for it are refused until the oldest
# of them is that old (or an admin unlocks it). 0 turns this off. Limit
# requests per address at nginx as well; the console counts per username only.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60

# Server mode: sign people out after this many seconds.
PERMANENT_SESSION_LIFETIME = 12 * 60 * 60

# Server mode, remote (https) servers: act on Incus with a certificate per user,
# restricted to the projects that user has access to, so Incus enforces the
# project boundary too. The console's own certificate must be unrestricted.
PER_USER_CERTIFICATES = True

# Seconds between refreshing the project list of an "all projects" user's
# certificate, so it picks up projects created outside the console.
CERT_SYNC_INTERVAL = 300
