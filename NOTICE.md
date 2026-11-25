# Notices

incus-console is licensed under the GNU General Public License, version 3 only
(see `LICENSE`).

## Third-party material

Files copied from [incus-ui-canonical](https://github.com/zabbly/incus-ui-canonical)
(a fork of Canonical's LXD UI, distributed under GPL-3.0 per its `LICENSE` file).
Copyright stays with that project's authors; see its repository for the holders.
List every file brought in from another project here.

| Path | Origin | Modified |
| ---- | ------ | -------- |
| `app/static/img/incus-logo.svg` | incus-ui-canonical `public/assets/img/incus-logo.svg` at commit 7e41404 | No |
| `app/static/img/favicon-32x32.png` | incus-ui-canonical `public/assets/img/favicon-32x32.png` at commit 7e41404 | No |
| `app/static/css/app.css` (font stacks, logo rules) | incus-ui-canonical `src/sass/_settings.scss` and `src/sass/_pattern_navigation.scss` at commit 7e41404 | Yes: rewritten as plain CSS |
| `app/templates/base.html` (application layout) | incus-ui-canonical `src/components/Navigation.tsx` at commit 7e41404 | Yes: rewritten as a Jinja template |

| `desktop/build/icon.png` | rendered from `app/static/img/incus-logo.svg` above | Yes: drawn at 1024 pixels |

## xterm.js

`app/static/vendor/xterm/` holds [xterm.js](https://github.com/xtermjs/xterm.js)
5.5.0 and its fit addon 0.10.0, MIT license (`app/static/vendor/xterm/LICENSE`),
copied from npm by `desktop/scripts/vendor-xterm.mjs`.

## Python in the desktop app

The desktop app ships a CPython 3.13 build from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
(Python Software Foundation License; the build project's own files are under its
licence), with this app's dependencies from `requirements.txt` installed in it.

## Vanilla Framework

`app/static/vendor/vanilla-framework-4.61.0.min.css` is Canonical's
[Vanilla Framework](https://github.com/canonical/vanilla-framework) 4.61.0,
LGPL-3.0, compiled from its npm package with Sass (see README), with its three
`@font-face` rules that load fonts from assets.ubuntu.com removed.
