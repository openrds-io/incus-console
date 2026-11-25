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
  rename, copy, snapshots, logs (console and Incus log files), a settings form
  for limits and common options, adding and removing devices, and full
  configuration editing as YAML
- Object storage: storage pools; buckets across all pools or per pool;
  create (credentials shown once), edit size and description, delete; access
  keys with admin or read-only roles, custom or generated access and secret
  keys, edit and delete
- Terminal: a root login shell in a running container (xterm.js over a websocket)
- Operations: list and cancel
- Projects: switch project per server

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

Create a user for server mode with `flask create-user NAME`.

Every user of a server-mode install acts on Incus with the console's one
client certificate, so treat every console user as an Incus administrator.

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

Use threads: each open terminal holds one for as long as it is open.

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
  db.py           SQLite: users and registered servers
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

1. Code signing and a build workflow for the desktop app on all three platforms
2. deb (Debian 13, Ubuntu 26.04) and rpm (EL10) packages with nginx and systemd
   units; container image
3. Later: virtual machines, networks, images, cluster, permissions, OIDC

## License

GPL-3.0-only. See `LICENSE` and `NOTICE.md`.
