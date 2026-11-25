# Notices

incus-console
Copyright 2026 Contributors to Incus Console (see `AUTHORS`)

Licensed under the Apache License, Version 2.0 (see `LICENSE`).
Per-file copyright and licensing is recorded in `REUSE.toml`.

## Third-party material

List every file brought in from another project here.

| Path | Origin | License | Modified |
| ---- | ------ | ------- | -------- |
| `app/static/img/logo.svg` | [lxc/incus-ui](https://github.com/lxc/incus-ui) `src/assets/logo.svg`, Copyright 2026 LinuxContainers contributors | Apache-2.0 | No |
| `desktop/build/icon.png` | rendered from `app/static/img/logo.svg` above | Apache-2.0 | Yes: drawn at 1024 pixels |
| `app/static/vendor/vanilla-framework-4.61.0.min.css` | Canonical's [Vanilla Framework](https://github.com/canonical/vanilla-framework) 4.61.0, compiled from its npm package with Sass (steps in `README.md`) | LGPL-3.0 | Yes: its three `@font-face` rules that load fonts from assets.ubuntu.com removed |
| `app/static/vendor/xterm/` | [xterm.js](https://github.com/xtermjs/xterm.js) 5.5.0 and its fit addon 0.10.0, copied from npm by `desktop/scripts/vendor-xterm.mjs` | MIT (`app/static/vendor/xterm/LICENSE`) | No |

The Incus name and logo identify the Incus project of Linux Containers. This
project is not affiliated with or endorsed by Linux Containers.

## Python in the desktop app

The desktop app ships a CPython 3.13 build from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
(Python Software Foundation License; the build project's own files are under its
licence), with this app's dependencies from `requirements.txt` installed in it.

## History

Releases before the switch to Apache-2.0 were GPL-3.0-only and included the logo,
favicon and some layout and style rules from
[incus-ui-canonical](https://github.com/zabbly/incus-ui-canonical). Those files
were removed or rewritten from scratch; none remain.
