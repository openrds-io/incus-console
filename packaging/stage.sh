# Shared by inside-deb.sh and inside-rpm.sh (sourced, inside the build container):
# lay out the files of an install under $STAGE.
#
#   /opt/incus-console/app, wsgi.py   the application
#   /opt/incus-console/venv           its Python packages and gunicorn
#   /etc/incus-console/config.py      settings (kept on upgrade)
#   /usr/lib/systemd/system/          incus-console.service
#   /usr/bin/incus-console            admin commands (create-user, ...)
#   /usr/sbin/incus-console-setup     first-time setup of nginx and the first admin
#   /usr/share/incus-console/         the nginx site that setup installs

PREFIX=/opt/incus-console

# The environment is built at its final path, so the paths inside it are right.
make_venv() {
  python3 -m venv "$PREFIX/venv"
  "$PREFIX/venv/bin/pip" install --no-cache-dir --upgrade pip
  "$PREFIX/venv/bin/pip" install --no-cache-dir -r /src/requirements.txt gunicorn
  # Nothing in the package should need pip again, or carry build leftovers.
  "$PREFIX/venv/bin/pip" uninstall -y pip >/dev/null
  find "$PREFIX/venv" -name __pycache__ -prune -exec rm -rf {} +
}

stage() {
  local src="$1" files="$1/packaging/files"
  rm -rf "$STAGE"
  install -d "$STAGE$PREFIX" "$STAGE/etc/incus-console" "$STAGE/usr/lib/systemd/system" \
    "$STAGE/usr/bin" "$STAGE/usr/sbin" "$STAGE/usr/share/incus-console" \
    "$STAGE/usr/share/doc/incus-console"
  cp -a "$src/app" "$STAGE$PREFIX/app"
  find "$STAGE$PREFIX/app" -name __pycache__ -prune -exec rm -rf {} +
  install -m 0644 "$src/wsgi.py" "$STAGE$PREFIX/wsgi.py"
  echo "$COMMIT_STAMP" > "$STAGE$PREFIX/app/COMMIT"
  [ ! -d "$PREFIX/venv" ] || cp -a "$PREFIX/venv" "$STAGE$PREFIX/venv"
  install -m 0640 "$files/config.py" "$STAGE/etc/incus-console/config.py"
  install -m 0644 "$files/incus-console.service" "$STAGE/usr/lib/systemd/system/"
  install -m 0755 "$files/incus-console" "$STAGE/usr/bin/incus-console"
  install -m 0755 "$files/incus-console-setup" "$STAGE/usr/sbin/incus-console-setup"
  install -m 0644 "$files/nginx-site.conf" "$STAGE/usr/share/incus-console/nginx-site.conf"
  install -m 0644 "$src/README.md" "$src/LICENSE" "$src/NOTICE.md" "$STAGE/usr/share/doc/incus-console/"
}
