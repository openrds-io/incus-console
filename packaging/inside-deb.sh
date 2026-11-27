#!/usr/bin/env bash
# Runs inside a Debian or Ubuntu container (see build.sh): builds the .deb.
set -euo pipefail

STAGE=/tmp/stage
# shellcheck source=packaging/stage.sh
. /src/packaging/stage.sh

control() {
  local arch="$1" python="$2" next="$3" size
  size="$(du -sk "$STAGE" | cut -f1)"
  install -d "$STAGE/DEBIAN"
  cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: incus-console
Version: $VERSION
Architecture: $arch
Maintainer: Contributors to Incus Console <https://github.com/openrds-io/incus-console>
Installed-Size: $size
Depends: python3 (>= $python), python3 (<< $next), nginx, openssl, adduser, systemd
Section: admin
Priority: optional
Homepage: https://github.com/openrds-io/incus-console
Description: A desktop and web console for Incus by your friends at OpenRDS
 Incus Console manages Incus containers, storage, networks, projects and
 clusters from a browser, with users, roles and an audit log.
 .
 After installing, run incus-console-setup to put it behind nginx and create
 the first admin.
CONTROL
  echo "/etc/incus-console/config.py" > "$STAGE/DEBIAN/conffiles"
  install -m 0755 /src/packaging/files/postinst "$STAGE/DEBIAN/postinst"
  install -m 0755 /src/packaging/files/prerm "$STAGE/DEBIAN/prerm"
  install -m 0755 /src/packaging/files/postrm "$STAGE/DEBIAN/postrm"
}

main() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q --no-install-recommends python3 python3-venv ca-certificates
  local arch python next
  arch="$(dpkg --print-architecture)"
  python="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  next="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1] + 1}")')"
  make_venv
  stage /src
  control "$arch" "$python" "$next"
  dpkg-deb --root-owner-group --build "$STAGE" "/out/incus-console_${VERSION}_${arch}~${LABEL}.deb"
}

# Sourced by a test to try the steps one at a time; run directly, it builds.
[ "${BASH_SOURCE[0]}" != "$0" ] || main "$@"
