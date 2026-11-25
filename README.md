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
  snapshots, and full configuration editing as YAML
- Object storage: pools, buckets, and bucket access keys
- Operations: list and cancel
- Projects: switch project per server

Not built yet: the container terminal and logs, the Electron runtime, and the
deb, rpm and container packages. See [Roadmap](#roadmap).

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
| Web server | Flask's own, bound to 127.0.0.1 | Apache with mod_wsgi (deb, rpm); gunicorn (container) |

Create a user for server mode with `flask create-user NAME`.

Every user of a server-mode install acts on Incus with the console's one
client certificate, so treat every console user as an Incus administrator.

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
  incus/client.py Incus REST client (standard library only)
  views/          pages: servers, instances, storage, operations
  templates/      Jinja templates
  static/         CSS and a few lines of JavaScript
tests/
  fake_incus.py   in-memory Incus API used by the tests and for demos
wsgi.py           entry point for mod_wsgi and gunicorn
```

## Roadmap

1. Container terminal and logs (Socket.IO: WebSocket under gunicorn and
   Electron, long-polling under mod_wsgi)
2. Electron runtime with a bundled Python
3. deb (Debian 13, Ubuntu 26.04) and rpm (EL10) packages; container image
4. Graphics and styling from incus-ui-canonical (see `NOTICE.md`)
5. Later: virtual machines, networks, images, cluster, permissions, OIDC

## License

GPL-3.0-only. See `LICENSE` and `NOTICE.md`.
