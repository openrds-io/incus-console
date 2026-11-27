#!/usr/bin/env bash
# Build Incus Console's server packages: deb, rpm and a container image.
#
# Each package is built inside a container of the distribution it is for, so
# its bundled Python environment matches that distribution's Python. Needs
# docker or podman (on a Mac: Colima or Docker Desktop).
#
#   packaging/build.sh debian13      -> dist/incus-console_<version>_<arch>~debian13.deb
#   packaging/build.sh ubuntu2604    -> dist/incus-console_<version>_<arch>~ubuntu2604.deb
#   packaging/build.sh el10          -> dist/incus-console-<version>-1.el10.<arch>.rpm
#   packaging/build.sh image         -> container image incus-console:<version>
#   packaging/build.sh all           -> all of the above
#
# Settings (environment): CONTAINER_TOOL=docker|podman  PLATFORM=linux/amd64
# (default: this machine's own architecture; packages are per architecture).
# Building for the other architecture runs under emulation: it works, slowly.
#   PLATFORM=linux/amd64 packaging/build.sh all

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
DIST="$ROOT/dist"

die() { echo "build.sh: $*" >&2; exit 1; }

TOOL="${CONTAINER_TOOL:-}"
if [ -z "$TOOL" ]; then
  if command -v docker >/dev/null; then TOOL=docker
  elif command -v podman >/dev/null; then TOOL=podman
  else die "docker or podman is needed"; fi
fi

VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/app/version.py")"
[ -n "$VERSION" ] || die "no version in app/version.py"

# The commit being packaged, for the About box: "<commit>" or "<commit> release"
# when the checkout is exactly this version's tag (as desktop/scripts/stamp-commit.mjs).
stamp() {
  local commit tagged dirty
  commit="$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
  tagged="$(git -C "$ROOT" describe --tags --exact-match 2>/dev/null || true)"
  dirty="$(git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null || echo dirty)"
  if [ "$tagged" = "v$VERSION" ] && [ -z "$dirty" ]; then echo "$commit release"; else echo "$commit"; fi
}

# (Written so that the empty case also works with the bash 3.2 that macOS ships.)
PLATFORM_ARGS=()
[ -z "${PLATFORM:-}" ] || PLATFORM_ARGS=(--platform "$PLATFORM")

# Run the inside-the-container half for one distribution.
package() {
  local image="$1" family="$2" label="$3"
  mkdir -p "$DIST"
  echo "==> $label ($image)"
  "$TOOL" run --rm ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} \
    -v "$ROOT:/src:ro" -v "$DIST:/out" \
    -e VERSION="$VERSION" -e COMMIT_STAMP="$(stamp)" -e LABEL="$label" \
    "$image" bash "/src/packaging/inside-$family.sh"
}

image() {
  # An image for another architecture gets that in its tag, and is not "latest".
  local tag="incus-console:$VERSION" latest=(-t incus-console:latest)
  if [ -n "${PLATFORM:-}" ]; then
    tag="$tag-${PLATFORM#linux/}"
    latest=()
  fi
  echo "==> container image $tag"
  "$TOOL" build ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} -f "$HERE/Containerfile" \
    --build-arg COMMIT_STAMP="$(stamp)" \
    -t "$tag" ${latest[@]+"${latest[@]}"} "$ROOT"
}

case "${1:-}" in
  debian13) package debian:13 deb debian13 ;;
  ubuntu2604) package ubuntu:26.04 deb ubuntu2604 ;;
  el10) package almalinux:10 rpm el10 ;;
  image) image ;;
  all)
    package debian:13 deb debian13
    package ubuntu:26.04 deb ubuntu2604
    package almalinux:10 rpm el10
    image
    ;;
  *) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
[ "${1:-}" = image ] || ls -l "$DIST"
