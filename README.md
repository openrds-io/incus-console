# Incus Console

A desktop and web console for [Incus](https://linuxcontainers.org/incus/) by your friends at
[OpenRDS](https://github.com/openrds-io).
It is a front end to the Incus REST API: a small Python 3.13 / Flask app that
renders pages on the server and talks to Incus over a local unix socket or HTTPS.

It manages Incus containers: **system containers** and **OCI application
containers**, side by side in one list and with the same pages, on any number
of Incus servers. Virtual machines are out of scope: those on a server are
left alone and not shown. It also covers storage (including S3-compatible
object storage), networks, profiles, images, projects, and users with roles.

## Status

Working today:

- Servers: register a local socket, or a remote server with a trust token
- Clusters: register any one member and the console works with the whole
  cluster. A Cluster page lists the members (address, roles, groups, status,
  instances); on a member's own page, console admins evacuate it before
  maintenance and restore it afterwards. Instances and volumes show the member they are on; the create
  forms let you choose it (or leave it to Incus); a stopped instance moves to
  another member; new networks are defined on every member. Each member has
  a page with its load, memory, storage pool usage and instances, and admins
  edit its description, failure domain, groups and roles. Cluster groups:
  create, rename, choose members, delete; an instance or a copy of one can
  be placed on a member or on any member of a group. Admins can remove an
  empty member from the cluster
- Instances, of either kind: list, start, stop, restart, freeze, delete,
  rename, copy, snapshots (by hand or on a schedule, with expiry), logs
  (console and Incus log files), a settings form for limits and common
  options, adding and removing devices, full configuration editing as YAML,
  and publishing a stopped instance as an image
- Creating: one form with a tab per kind. Instances that wait on a large
  image download show that they are working, and carry on under Operations
  when they outlast the page
- System containers: create from the image server's catalog, an alias or a
  local image, with a storage pool and cloud-init user data; settings for
  nesting, privileged mode and process limits
- Application containers (OCI): create from Docker Hub, ghcr.io, quay.io or
  another registry (the Incus server pulls the image), with environment
  variables, published ports (`8080:80`) and volumes (`data:/var/lib/app`,
  created when new); a login for private registries; "Use the usual
  settings" fills these in for well-known images (`OCI_PRESETS`); and
  **Update image** rebuilds a stopped container on a newer image, keeping
  its settings and volumes. The command, working directory, user and
  environment are editable in Settings, and the terminal runs `sh`.
  Environment values are hidden from Viewers and Operators, since they often
  hold passwords. Needs
  Incus 6.3 or later with `skopeo` and `umoci` on the Incus server; the console
  offers it only where the server supports it
- Consoles: a text console for every running instance (boot messages, then
  the login prompt), opening with what the instance printed before you
  attached
- Terminal: a root shell in a running instance (xterm.js over a websocket)
- Files: browse an instance's folders, view and edit text files, download,
  upload, create folders and delete
- Profiles: list, create (empty or as a copy), settings form, devices, YAML,
  rename, delete
- Images: list, details, aliases, delete, and creating an instance of the
  right kind from an image
- Networks: list, create bridges, settings, YAML, rename, delete, DHCP leases,
  and port forwards from a host address to instances
- Volumes: custom storage volumes across pools; create, resize, rename, delete,
  attach to and detach from instances, and snapshots (also scheduled)
- Storage pools: list, details with space used, and (console admins) create
  with the dir, ZFS, Btrfs or LVM driver and delete. In a cluster the pool is
  defined on every member with its own source, and its page shows the source
  and space per member; a network's page shows its state on each member
- Object storage: buckets across all pools or per pool;
  create (credentials shown once), edit size and description, delete; access
  keys with admin or read-only roles, custom or generated access and secret
  keys, edit and delete
- Operations: list and cancel; actions that outlast the page are watched in the
  background and their outcome is reported, even after Incus forgets them
- Projects: list, create and configure (features, resource limits, restrictions),
  usage against limits, YAML, rename, delete, and switch project per server
- Users and access (server mode): console admins, and Viewer, Operator or
  Project admin roles per server and project, given to users directly or
  through groups; user and group management, own password change, and an
  audit log that filters by user, server, action and date and exports as CSV.
  Repeated failed sign-ins lock a username for a while; a password change
  ends the user's other sessions, and admins can sign a user out everywhere
- Live updates: lists, detail pages and Operations refresh themselves when
  something changes on the server (Incus's event stream, relayed as
  server-sent events), but never while you are typing in a form

Server installs come as deb and rpm packages and a container image: see
[Packages](#packages-server-mode).

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

Add `--cluster` and the fake is a three-member cluster.

### A real cluster on a Mac

`dev/cluster.sh` builds a small Incus cluster out of [Lima](https://lima-vm.io)
virtual machines (`brew install lima`; Colima already brought it along). Each
member runs Debian 13 with Incus from the Zabbly repository, and has an
address on Lima's vzNAT network that the Mac can reach.

macOS keeps vzNAT machines from talking to each other directly, so the members
reach each other through the Mac, which has to forward their packets. That is
off by default and resets when the Mac restarts; `up` tells you when it is
needed:

```bash
sudo sysctl -w net.inet.ip.forwarding=1
```

```bash
dev/cluster.sh up 3      # create incus1..incus3 and form the cluster
dev/cluster.sh token     # a trust token: paste it under Servers > Remote, with a trust token
dev/cluster.sh status
dev/cluster.sh stop      # "up" starts them again; "delete" removes them
```

Start the cluster with `up` rather than `limactl start`: it also sets the
members' routes. If members cannot reach each other, `dev/cluster.sh doctor`
prints what each one can and cannot reach.

Each member takes 2 CPUs, 2 GiB of memory and up to 20 GiB of disk; see the
top of the script for the settings. The pool is a plain directory on each
member and `incusbr0` is a separate bridge per member, so containers on
different members do not see each other: enough for the console's pages, not
a model for production.

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

**Groups** (admins only) bundle roles for many users at once. A user's access
is their own roles plus those of every group they are in, again with the
strongest winning. Adding someone to a group, removing them, or changing the
group's roles takes effect at once, including their restricted certificates
below. Groups carry project roles only: being a console admin is always set
per user. Deleting a group takes away what it gave, not its members' own
roles.

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

### Permissions: still to do

Pending work, roughly in the order planned:

- [ ] **OIDC sign-in:** log in with the organisation's identity provider
  (Keycloak, Entra ID, Okta, ...); map identity-provider groups to console
  groups (`user_groups.external_name` is reserved for this); local accounts
  stay available
- [ ] **Incus-enforced roles inside a project:** an authorization scriptlet
  shipped with the console, so Incus also refuses a Viewer's or Operator's
  changes, not just the console
- [ ] **Per-instance access:** grant a role on one instance (needs the
  scriptlet or OpenFGA)
- [ ] **Access tab:** show who can reach a project or instance, from Incus's
  `/1.0/projects/<name>/access` and `/1.0/instances/<name>/access`
- [ ] **Sign-in hardening:** optional TOTP second factor for local accounts;
  limiting attempts per address (today: per username, see below)
- [ ] **Per-user certificates:** rotate them before they expire (today they
  last 10 years); a "re-sync" button per user and per server; tidy up trust
  entries left behind when a server was unreachable
- [ ] **Audit log:** optionally also send entries to syslog or the journal
- [ ] **Later, if needed:** OIDC tokens passed through to Incus (Incus sees the
  real user, with OpenFGA syncing grants); LDAP or a trusted proxy header
  (`X-Remote-User`) for single sign-on at nginx; letting non-admins create
  projects (decided against for now)

The audit log (**Audit log**, admins only) records every change made through
the console with its outcome, sign-ins, failed sign-ins and opened terminals.
`AUDIT_KEEP_DAYS` (default 365) sets how long entries are kept. Sessions end
after `PERMANENT_SESSION_LIFETIME` seconds (default 12 hours), and disabling a
user signs them out at once. So does **Sign out everywhere** on the user's
page, and a password change or reset ends every other session of that user.

After `LOGIN_MAX_FAILURES` failed sign-ins (default 5) for one username within
`LOGIN_LOCKOUT_SECONDS` (default 15 minutes), further attempts for that
username are refused until the oldest of them is that old, or an admin clicks
**Unlock** on the user's page. Names that are not users are treated the same,
so the answer does not reveal which usernames exist. The count is per
username; limit requests per address at nginx (`limit_req`) as well.

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

## Packages (server mode)

`packaging/build.sh` builds the server packages, each inside a container of
the distribution it is for (docker or podman needed; on a Mac, Colima):

```bash
packaging/build.sh debian13     # dist/incus-console_<version>_<arch>~debian13.deb
packaging/build.sh ubuntu2604   # dist/incus-console_<version>_<arch>~ubuntu2604.deb
packaging/build.sh el10         # dist/incus-console-<version>-1.el10.<arch>.rpm
packaging/build.sh image        # container image incus-console:<version>
```

A package is for the architecture it was built on; set
`PLATFORM=linux/amd64` to build for another. Like pgAdmin's, it carries its
own Python environment (`/opt/incus-console/venv`), so it depends only on the
distribution's `python3`, `nginx` and `openssl`.

On the server:

```bash
sudo apt install ./incus-console_*.deb     # or: sudo dnf install ./incus-console-*.rpm
sudo incus-console-setup
```

`incus-console-setup` makes a self-signed certificate, installs the nginx site
(ports 80 and 443), gives the console access to an Incus on the same machine
(the `incus-admin` group), starts the services and offers to create the first
console admin. It can be run again; it keeps what is there.

| | |
|---|---|
| Settings | `/etc/incus-console/config.py` (kept on upgrade), then `systemctl restart incus-console` |
| Data | `/var/lib/incus-console`: database, secret key, client certificate |
| Service | `incus-console.service`: gunicorn on 127.0.0.1:5002, as the `incus-console` user |
| nginx | `/etc/nginx/conf.d/incus-console.conf`; certificate in `/etc/incus-console/tls` |
| Users | `sudo incus-console create-user NAME [--admin]` |
| Logs | `journalctl -u incus-console` |

`dev/package-test.sh debian13` (or `ubuntu2604`, `el10`) tries a built package
for real: it installs it in a fresh container on the development cluster
(`dev/cluster.sh`), runs `incus-console-setup`, checks that the login page
answers through nginx, and publishes the console on a port of the first
member for a look in the browser.

The container image runs gunicorn alone on port 5002 with its data in `/data`;
put a proxy that serves HTTPS in front of it, as below:

```bash
docker run -d --name incus-console -p 127.0.0.1:5002:5002 -v incus-console:/data incus-console:<version>
docker exec -it incus-console flask --app wsgi create-user NAME --admin
```

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

The pages use Canonical's Vanilla Framework (LGPL-3.0) with the Incus logo from
[lxc/incus-ui](https://github.com/lxc/incus-ui) (Apache-2.0); see `NOTICE.md`. The compiled CSS is checked
in at `app/static/vendor/`. To rebuild it (Node needed):

```bash
mkdir -p /tmp/vanilla-build && cd /tmp/vanilla-build
npm init -y >/dev/null
npm install --no-save vanilla-framework@4.61.0 sass@1.97.2
printf '@import "vanilla-framework";\n@include vanilla;\n' > vanilla.scss
npx sass --load-path=node_modules --style=compressed --no-source-map --quiet-deps \
  vanilla.scss vanilla.css
# Drop the @font-face rules that load fonts from assets.ubuntu.com; app.css
# uses fonts installed on the machine.
python3 -c "import re,sys; p=sys.argv[1]; s=open(p,encoding='utf-8-sig').read(); open(p,'w').write(re.sub(r'@font-face\{[^}]*assets\.ubuntu\.com[^}]*\}','',s))" vanilla.css
cp vanilla.css <repo>/app/static/vendor/vanilla-framework-4.61.0.min.css
```

`app/static/css/app.css` holds this console's own additions.

## Versions and About

Versions follow Ubuntu's style, `YY.MM.N`: the year and month of the release
series, then the release within it (26.11.1, 26.11.2, ...). A new feature
release takes the month it ships in. The number lives in `app/version.py` and
`desktop/package.json` (a test keeps them equal); releases are tagged
`v<version>`. Anything else shows as `<version>-dev` with its commit.
Packaging records the commit in `app/COMMIT` (`npm run stamp` for the desktop
app), since installs have no git checkout.

**About** (sidebar, the footer's version, or Help → About in the desktop app)
shows the version, commit, mode, signed-in user, browser and the current Incus
server, with **Copy details** for bug reports. Admins also see the
installation's paths, certificate fingerprint and software versions.

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
  db.py           SQLite: users, groups, grants, audit log, registered servers
  servers.py      server registry
  certs.py        client certificate
  terminal.py     browser terminal relay (simple-websocket)
  desktop.py      the server the desktop app starts
  incus/client.py Incus REST client (standard library, plus websocket-client)
  views/          pages: servers, cluster, instances, storage, operations
  templates/      Jinja templates
  static/         CSS and a few lines of JavaScript
tests/
  fake_incus.py   in-memory Incus API used by the tests and for demos
dev/cluster.sh    a small Incus cluster in Lima virtual machines, for development
dev/package-test.sh  installs a built package in a fresh container on that cluster
packaging/        deb, rpm and container image: build.sh, and the files they install
desktop/          Electron app (main.js) and its build scripts
wsgi.py           entry point for gunicorn
```

## Roadmap

1. Clusters, next steps: member-specific network settings (such as a
   bridge's external interfaces), shared storage drivers (Ceph), and OVN
   networks. Copying and moving between separately registered servers is out
   of scope for now
2. Packages, next steps: a package repository and signing; amd64 builds as
   part of a release
3. Test the desktop installers (`npm run dist`); code signing and a build
   workflow for all three platforms
4. OCI application containers, next steps: checking for a newer image without
   rebuilding; health and restart policy
5. More permissions work: see [Permissions: still to do](#permissions-still-to-do)

## License

Apache-2.0. Copyright 2026 Contributors to Incus Console. See `LICENSE` and
`NOTICE.md`; per-file licensing is recorded in `REUSE.toml`.
