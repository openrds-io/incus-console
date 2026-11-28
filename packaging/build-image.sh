# Sourced by build.sh and repo.sh: the Debian 13 build image, made once a week,
# with the updates and build tools already in it. Needs TOOL (docker or podman)
# and PLATFORM_ARGS. BUILD_REFRESH=1 makes a fresh one now. Only Debian 13 has
# one for now; the other distributions build in their plain images, as before.

# What the builds install: kept in step with inside-deb.sh and inside-repo.sh,
# which still install anything missing, so a plain image works too.
build_image_recipe() {
  echo "apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y -q && \
DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
python3 python3-venv ca-certificates reprepro gnupg && apt-get clean && rm -rf /var/lib/apt/lists/*"
}

# Prints the image to build in for a label, making it if needed.
build_image() {
  local label="$1" base="$2" week recipe tag old
  week="$(date +%G-w%V)"
  # A change to the recipe makes a new image at once, not next week.
  recipe="$(build_image_recipe | cksum | cut -d' ' -f1)"
  tag="incus-console-build:$label-$week-$recipe"
  [ -z "${PLATFORM:-}" ] || tag="$tag-${PLATFORM#linux/}"
  if [ "${BUILD_REFRESH:-}" = 1 ] || ! "$TOOL" image inspect "$tag" >/dev/null 2>&1; then
    echo "==> build image $tag (from $base, this week's updates)" >&2
    printf 'FROM %s\nRUN %s\n' "$base" "$(build_image_recipe "$label")" |
      "$TOOL" build ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} --pull -t "$tag" - >&2
    # Last week's image for this label is no use any more.
    for old in $("$TOOL" images --format '{{.Repository}}:{{.Tag}}' |
      grep "^incus-console-build:$label-" | grep -vx "$tag" || true); do
      "$TOOL" rmi "$old" >/dev/null 2>&1 || true
    done
  fi
  echo "$tag"
}
