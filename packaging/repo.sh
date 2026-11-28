#!/usr/bin/env bash
# A signed apt repository of OpenRDS packages, laid out the way pkgs.zabbly.com is:
#
#   dist/repo/key.asc                                  the key that signs it
#   dist/repo/<product>/<channel>/dists/trixie/...     what apt reads
#   dist/repo/<product>/<channel>/pool/...             the packages
#
# The whole dist/repo tree is static files: copy it to any web host (S3 and
# CloudFront, say) and point pkgs.openrds.io at it.
#
#   packaging/repo.sh build [channel]   add the newest dist/*~debian13.deb (default: testing)
#   packaging/repo.sh serve             serve dist/repo on port 8080 for a quick try
#   packaging/repo.sh try MACHINE [channel]
#                                       install incus-console on a Lima machine from it
#
# reprepro and gpg run in a Debian container, as the packages are built: docker
# or podman is needed, nothing else. The signing key lives outside the repo, in
# $OPENRDS_REPO_GNUPG (default ~/.openrds-repo-gnupg); it is made on first use.
# Keep a copy of that folder somewhere safe: a new key means every user has to
# fetch key.asc again.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
DIST="$ROOT/dist"
PRODUCT="incus-console"
PORT=8080
GNUPG="${OPENRDS_REPO_GNUPG:-$HOME/.openrds-repo-gnupg}"

die() { echo "repo.sh: $*" >&2; exit 1; }

container_tool() {
  if [ -n "${CONTAINER_TOOL:-}" ]; then echo "$CONTAINER_TOOL"
  elif command -v docker >/dev/null; then echo docker
  elif command -v podman >/dev/null; then echo podman
  else die "docker or podman is needed"; fi
}

channel_ok() {
  [[ "$1" =~ ^[a-z][a-z0-9.-]*$ ]] || die "a channel is a name such as testing or stable"
}

build() {
  local channel="${1:-testing}" newest
  channel_ok "$channel"
  # One version per suite in the repository: the newest built package.
  newest="$(ls "$DIST"/"$PRODUCT"_*~debian13.deb 2>/dev/null | sort -V | tail -n1 || true)"
  [ -n "$newest" ] || die "no $PRODUCT package in dist/. Run: packaging/build.sh debian13"
  mkdir -p "$GNUPG" "$DIST/repo" "$DIST/repo-state"
  chmod 700 "$GNUPG"
  echo "==> $(basename "$newest") into $PRODUCT/$channel"
  "$(container_tool)" run --rm \
    -v "$HERE:/packaging:ro" -v "$DIST:/dist" -v "$GNUPG:/gnupg" \
    -e PRODUCT="$PRODUCT" -e CHANNEL="$channel" -e DEB="/dist/$(basename "$newest")" \
    debian:13 bash /packaging/inside-repo.sh
  echo
  echo "Repository: dist/repo/$PRODUCT/$channel (key: dist/repo/key.asc)"
  echo "Try it: packaging/repo.sh serve, then in another Terminal: packaging/repo.sh try incus4 $channel"
}

serve() {
  [ -d "$DIST/repo" ] || die "nothing to serve yet. Run: packaging/repo.sh build"
  echo "Serving dist/repo at http://0.0.0.0:$PORT/ (Lima machines reach it at 192.168.64.1). Ctrl-C stops."
  exec python3 -m http.server "$PORT" --bind 0.0.0.0 --directory "$DIST/repo"
}

try() {
  local machine="${1:-}" channel="${2:-testing}"
  [ -n "$machine" ] || die "try takes a Lima machine, such as incus4 (dev/cluster.sh spare 4 makes one)"
  channel_ok "$channel"
  command -v limactl >/dev/null || die "limactl not found"
  # What a user would do, with the Mac standing in for pkgs.openrds.io.
  limactl shell --workdir / "$machine" sudo env CHANNEL="$channel" PRODUCT="$PRODUCT" \
    URL="http://192.168.64.1:$PORT" bash -s <<'REMOTE'
set -eu
mkdir -p /etc/apt/keyrings
curl -fsSL "$URL/key.asc" -o /etc/apt/keyrings/openrds.asc
cat > "/etc/apt/sources.list.d/openrds-$PRODUCT-$CHANNEL.sources" <<SOURCES
Enabled: yes
Types: deb
URIs: $URL/$PRODUCT/$CHANNEL
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/openrds.asc
SOURCES
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$PRODUCT"
dpkg-query -W -f='Installed: ${Package} ${Version}\n' "$PRODUCT"
REMOTE
}

case "${1:-}" in
  build) build "${2:-}" ;;
  serve) serve ;;
  try) try "${2:-}" "${3:-}" ;;
  *) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
