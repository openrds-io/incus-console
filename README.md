# incus-console

A lean web and desktop console for [Incus](https://linuxcontainers.org/incus/).
It is a front end to the Incus REST API: a small Python 3.13 / Flask app (the same
stack as openrds-io/ui) that renders pages on the server and talks to Incus over a local unix socket or HTTPS.

The feature set follows [incus-ui-canonical](https://github.com/zabbly/incus-ui-canonical).
System containers come first, object storage second.

## Status

Early scaffold. Working today:

- Servers: register a local socket, or a remote server with a trust token
- System containers: list, create, start, stop, restart, freeze, delete,
  rename, copy, snapshots (by hand or on a schedule, with expiry), logs (console and Incus log files), a settings form
  for limits and common options, adding and removing devices, and full
  configuration editing as YAML; creating from the image server's catalog,
  with a storage pool and cloud-init user data
- Files: browse a container's folders, view and edit text files, download,
  upload, create folders and delete
- Profiles: list, create (empty or as a copy), settings form, devices, YAML,
  rename, delete
- Images: list, details, aliases, delete, and publishing a stopped container as
  an image
- Networks: list, create bridges, settings, YAML, rename, delete, DHCP leases,
  and port forwards from a host address to containers
- Volumes: custom storage volumes across pools; create, resize, rename, delete,
  attach to and detach from containers, and snapshots (also scheduled)
- Object storage: storage pools; buckets across all pools or per pool;
  create (credentials shown once), edit size and description, delete; access
  keys with admin or read-only roles, custom or generated access and secret
  keys, edit and delete
- Terminal: a root login shell in a running container (xterm.js over a websocket)
- Operations: list and cancel; actions that outlast the page are watched in the
  background and their outcome is reported, even after Incus forgets them
- Projects: list, create and configure (features, resource limits, restrictions),
  usage against limits, YAML, rename, delete, and switch project per server
- Users and access (server mode): console admins, and Viewer, Operator or
  Project admin roles per server and project; user management, own password
  change, and an audit log
- Live updates: lists, detail pages and Operations refresh themselves when
  something changes on the server (Incus's event stream, relayed as
  server-sent events), but never while you are typing in a form

Not built yet: the deb, rpm and container packages. See [Roadmap](#roadmap).

## Quick start (development)

```bash
python3.13 -m venv --clear .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
INCUS_CONSOLE_SERVER_MODE=false flask run --debug --port 5002
```

Open http://127.0.0.1:5002 and add a server. Sockets found on the machine
(including Colima's `~/.colima/default/incus.sock`) are offered with one click.

No Incus at hand? Run the in-memory fake and add its socket path as a local server:

```bash
python -m tests.fake_incus /tmp/fake-incus.sock
```

Run the checks with `ruff check .` and `pytest`.

## Two modes

The same app runs in two modes, following pgAdmin's model.

| | Desktop | Server |
| --- | --- | --- |
| Setting | `SERVER_MODE = False` | `SERVER_MODE = True` (default) |
| Shipped as | Electron app (Windows, macOS, Linux) | deb, rpm, container image |
| Access | No login. The runtime starts the server on a random local port and proves itself with `DESKTOP_KEY`. | Username and password. |
| Web server | Flask's own, bound to 127.0.0.1 | gunicorn behind nginx |

Create the first user for server mode with `flask create-user NAME`. The
first user is always a console admin; add `--admin` for more admins. After
that, admins manage users under **Users**.

## Users and access (server mode)

| Role | Can |
| --- | --- |
| Console admin | Everything: servers, users, the audit log, creating and configuring projects |
| Project admin | Everything inside the project: instances, profiles, images, networks, volumes, buckets |
| Operator | Viewer, plus start, stop, restart, freeze, snapshots, terminal and files |
| Viewer | See everything in the project; change nothing; no terminal or files |

Roles are given per server, for one project or for all projects on that
server. The strongest one that applies wins. Users see only the servers and
projects they have access to, and buttons they cannot use are hidden. Only
console admins create projects or change a project's limits and restrictions.

The console checks every request. On remote (https) servers it also gives
each user who is not an admin their own client certificate, added to that
server's trust store as a restricted certificate (`incus-console/<username>`)
listing the user's projects. That user's requests go out with it, so Incus
itself refuses other projects, project settings and server-wide changes, even
if the console got something wrong. The certificate is updated or removed when
access changes, the user is disabled or deleted, a project is created, renamed
or deleted, or the server is removed. "All projects" certificates also pick up
projects made outside the console (`CERT_SYNC_INTERVAL`, default 5 minutes).

What this does not cover:

- Roles inside a project (Viewer, Operator) are enforced by the console only;
  Incus sees a user who may do everything in their projects.
- Local-socket servers have no per-user identity; the console is the only check.
- The console's own certificate must be unrestricted, since it manages the
  trust store, and the per-user keys are stored in `DATA_DIR/user-certs`.
  Protect `DATA_DIR` like any other credential store.

Set `PER_USER_CERTIFICATES = False` to act on every server with the console's
certificate alone.

The audit log (**Audit log**, admins only) records every change made through
the console with its outcome, sign-ins, failed sign-ins and opened terminals.
`AUDIT_KEEP_DAYS` (default 365) sets how long entries are kept. Sessions end
after `PERMANENT_SESSION_LIFETIME` seconds (default 12 hours), and disabling a
user signs them out at once.

## Desktop app

`desktop/` is an Electron app on pgAdmin's model: it starts this Flask app with
`python -m app.desktop` on a free port on 127.0.0.1, with a random key for that
launch, and shows it in a window. Nothing else on the machine can use the
console without that key. Its data (servers, client certificate) is in the
app's own folder; File > Data folder opens it, and File > Server log shows the
server's output.

Run it from a checkout (uses the repository's `.venv`):

```bash
cd desktop
npm install
npm run vendor-xterm   # once; copies xterm.js into app/static/vendor/xterm
npm start
```

Build an installer for the machine you are on (a DMG on macOS, an NSIS installer
on Windows, AppImage, deb and rpm on Linux):

```bash
cd desktop
npm install
npm run dist           # downloads a standalone Python 3.13, then builds into dist/
```

`npm run dist` bundles a Python for the OS and CPU it runs on, so build each
platform on that platform. The builds are not code-signed: macOS asks you to
right-click and Open the first time, and Windows SmartScreen warns.

## Behind nginx (server mode)

Run the app under gunicorn and let nginx serve HTTPS in front of it. Set
`BEHIND_PROXY = True` so the app trusts nginx's `X-Forwarded-*` headers and
sends its cookie over HTTPS only.

```bash
pip install gunicorn
INCUS_CONSOLE_BEHIND_PROXY=true gunicorn --workers 2 --threads 50 --bind 127.0.0.1:5002 wsgi:app
```

Use threads: each open terminal, and each open page that updates live, holds
one for as long as it is open.

```nginx
location / {
    proxy_pass http://127.0.0.1:5002;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    # the terminal's websocket
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1h;
    # file browser uploads (MAX_CONTENT_LENGTH, default 1 GiB)
    client_max_body_size 1g;
}
```

## Look and feel

The pages use Canonical's Vanilla Framework, as incus-ui-canonical does, with
the Incus logo from that project (see `NOTICE.md`). The compiled CSS is checked
in at `app/static/vendor/`. To rebuild it (Node needed):

```bash
mkdir -p /tmp/vanilla-build && cd /tmp/vanilla-build
npm init -y >/dev/null
npm install --no-save vanilla-framework@4.61.0 sass@1.97.2
printf '@import "vanilla-framework";\n@include vanilla;\n' > vanilla.scss
npx sass --load-path=node_modules --style=compressed --no-source-map --quiet-deps \
  vanilla.scss vanilla.css
# Drop the @font-face rules that load fonts from assets.ubuntu.com; app.css
# uses local fonts first, like incus-ui-canonical.
python3 -c "import re,sys; p=sys.argv[1]; s=open(p,encoding='utf-8-sig').read(); open(p,'w').write(re.sub(r'@font-face\{[^}]*assets\.ubuntu\.com[^}]*\}','',s))" vanilla.css
cp vanilla.css <repo>/app/static/vendor/vanilla-framework-4.61.0.min.css
```

`app/static/css/app.css` holds this console's own additions.

## Configuration

Defaults are in `app/config.py`. Override them in
`/etc/incus-console/config.py` (or the file named by `INCUS_CONSOLE_CONFIG`),
or with `INCUS_CONSOLE_<NAME>` environment variables.

The database, session secret and client certificate live in `DATA_DIR`
(default `~/.incus-console`).

## Layout

```
app/
  __init__.py     app factory
  config.py       default settings
  auth.py         login, desktop key, CSRF
  access.py       roles: who may do what on which server and project
  audit.py        audit log
  db.py           SQLite: users, grants, audit log, registered servers
  servers.py      server registry
  certs.py        client certificate
  terminal.py     browser terminal relay (simple-websocket)
  desktop.py      the server the desktop app starts
  incus/client.py Incus REST client (standard library, plus websocket-client)
  views/          pages: servers, instances, storage, operations
  templates/      Jinja templates
  static/         CSS and a few lines of JavaScript
tests/
  fake_incus.py   in-memory Incus API used by the tests and for demos
desktop/          Electron app (main.js) and its build scripts
wsgi.py           entry point for gunicorn
```

## Roadmap

1. About box; Ubuntu-style versions (YY.MM.N)
2. Virtual machines: create, VGA console, VM-specific settings
3. OCI application containers: create from OCI registries
4. Test the desktop installers (`npm run dist`); code signing and a build
   workflow for all three platforms
5. deb (Debian 13, Ubuntu 26.04) and rpm (EL10) packages with nginx and systemd
   units; container image
6. Later: OIDC sign-in, Incus-enforced viewer and operator roles (authorization
   scriptlet), per-instance access, cluster

## License

GPL-3.0-only. See `LICENSE` and `NOTICE.md`.
