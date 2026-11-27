#!/usr/bin/env bash
# Install a built package on a fresh system, to see that it really works.
#
# The fresh system is a container on the first member of the development
# cluster (dev/cluster.sh up). The package from dist/ is installed in it,
# incus-console-setup is run, and the console is published on a port of that
# member so a browser on the Mac can open it.
#
#   dev/package-test.sh debian13      (first: packaging/build.sh debian13)
#   dev/package-test.sh ubuntu2604
#   dev/package-test.sh el10
#   dev/package-test.sh clean         delete the test containers
#
# Settings (environment): CLUSTER_PREFIX=incus

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VM="${CLUSTER_PREFIX:-incus}1"

die() { echo "package-test.sh: $*" >&2; exit 1; }
say() { echo "==> $*"; }

command -v limactl >/dev/null || die "limactl not found"

# Nothing here reads the terminal, so pasted commands after this one survive.
root() { limactl shell --workdir / "$VM" sudo "$@" </dev/null; }
inside() { root incus exec "$NAME" -- "$@"; }

case "${1:-}" in
  debian13) IMAGE=images:debian/13 PATTERN='*~debian13.deb' PORT=8451 ;;
  ubuntu2604) IMAGE=images:ubuntu/26.04 PATTERN='*~ubuntu2604.deb' PORT=8452 ;;
  el10) IMAGE=images:almalinux/10 PATTERN='*.el10.*.rpm' PORT=8453 ;;
  clean)
    for target in debian13 ubuntu2604 el10; do
      root incus delete --force "pkgtest-$target" 2>/dev/null && echo "deleted pkgtest-$target" || true
    done
    exit 0
    ;;
  *) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
NAME="pkgtest-$1"

# The newest package of this kind.
PACKAGE="$(find "$ROOT/dist" -maxdepth 1 -name "$PATTERN" -print0 2>/dev/null | xargs -0 ls -t 2>/dev/null | head -n1)"
[ -n "$PACKAGE" ] || die "no $PATTERN in dist/. Build it first: packaging/build.sh $1"
FILE="$(basename "$PACKAGE")"

say "Copying $FILE to $VM"
limactl copy "$PACKAGE" "$VM:/tmp/$FILE" </dev/null

say "Starting a fresh $IMAGE container: $NAME"
root incus delete --force "$NAME" 2>/dev/null || true
root incus launch "$IMAGE" "$NAME" --target "$VM"

say "Waiting for its network"
for _ in $(seq 1 30); do
  inside getent hosts pkgs.zabbly.com >/dev/null 2>&1 && break
  sleep 2
done
inside getent hosts pkgs.zabbly.com >/dev/null || die "$NAME has no working DNS"

root incus file push "/tmp/$FILE" "$NAME/root/$FILE"

say "Installing $FILE"
case "$FILE" in
  *.deb) inside sh -c "apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q /root/'$FILE'" ;;
  *.rpm) inside dnf install -y -q "/root/$FILE" ;;
esac

say "Running incus-console-setup"
inside incus-console-setup

say "Checking the services"
# Through nginx, over HTTPS, as a browser would (the certificate is self-signed).
answers() {
  inside /opt/incus-console/venv/bin/python -c '
import ssl, urllib.request
context = ssl._create_unverified_context()
with urllib.request.urlopen("https://127.0.0.1/login", context=context, timeout=10) as page:
    body = page.read().decode()
    assert page.status == 200 and "Incus Console" in body, page.status
print("https://127.0.0.1/login answered", page.status)
' 2>/dev/null
}
ok=""
for _ in $(seq 1 10); do
  if answers; then ok=yes; break; fi
  sleep 2
done
if [ -z "$ok" ]; then
  echo "The console does not answer. What the services say:"
  inside systemctl --no-pager --lines=0 status incus-console nginx || true
  inside journalctl --no-pager -n 40 -u incus-console || true
  inside journalctl --no-pager -n 10 -u nginx || true
  die "$NAME is left running for a look: limactl shell --workdir / $VM sudo incus exec $NAME -- bash"
fi

say "Publishing it on port $PORT of $VM"
root incus config device add "$NAME" web proxy "listen=tcp:0.0.0.0:$PORT" connect=tcp:127.0.0.1:443

ADDRESS="$(limactl shell --workdir / "$VM" ip -4 -o addr show dev lima0 </dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
echo
echo "Installed and answering. Create an admin (it asks for a password):"
echo "    limactl shell --workdir / $VM sudo incus exec $NAME -t -- incus-console create-user admin --admin"
echo "Then open https://$ADDRESS:$PORT/ (self-signed certificate: the browser will ask)."
echo "When done: dev/package-test.sh clean"
