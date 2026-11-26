#!/usr/bin/env bash
# Runs inside an Enterprise Linux container (see build.sh): builds the .rpm.
set -euo pipefail

STAGE=/tmp/stage
# shellcheck source=packaging/stage.sh
. /src/packaging/stage.sh

main() {
  dnf install -y -q python3 python3-pip rpm-build systemd-rpm-macros
  local python
  python="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  make_venv
  stage /src
  rpmbuild -bb /src/packaging/incus-console.spec \
    --define "_topdir /tmp/rpmbuild" --define "_rpmdir /out" \
    --define "version $VERSION" --define "stage $STAGE" --define "python_minor $python" \
    --define "_rpmfilename %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm"
}

[ "${BASH_SOURCE[0]}" != "$0" ] || main "$@"
