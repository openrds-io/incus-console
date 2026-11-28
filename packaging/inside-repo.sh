#!/usr/bin/env bash
# The inside-the-container half of packaging/repo.sh: sign and add one package.
# In: PRODUCT, CHANNEL, DEB; /gnupg (the signing key), /dist (repo and repo-state).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
# The build image has these already; a plain one gets them now.
if ! command -v reprepro >/dev/null || ! command -v gpg >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq reprepro gnupg >/dev/null
fi

export GNUPGHOME=/gnupg
chmod 700 "$GNUPGHOME"
if ! gpg --list-secret-keys --with-colons 2>/dev/null | grep -q '^sec'; then
  echo "==> making the signing key (it is kept on the Mac, outside the repo)"
  gpg --batch --passphrase '' --quick-gen-key "OpenRDS packages <packages@openrds.io>" ed25519 sign never
fi
key="$(gpg --list-secret-keys --with-colons | awk -F: '/^fpr/ {print $10; exit}')"

# reprepro's own records stay out of the tree that gets published.
state="/dist/repo-state/$PRODUCT/$CHANNEL"
out="/dist/repo/$PRODUCT/$CHANNEL"
mkdir -p "$state/conf" "$out"
cat > "$state/conf/distributions" <<CONF
Origin: OpenRDS
Label: OpenRDS $PRODUCT
Codename: trixie
Architectures: amd64 arm64
Components: main
Description: OpenRDS packages: $PRODUCT, $CHANNEL channel, Debian 13
SignWith: $key
CONF

# One version per suite: a rebuild of the same version replaces the old one
# (builds are not byte-for-byte the same yet, and reprepro refuses a second,
# different file under one name). Published channels should get new versions.
reprepro -b "$state" --outdir "$out" remove trixie "$PRODUCT" >/dev/null 2>&1 || true
reprepro -b "$state" --outdir "$out" deleteunreferenced >/dev/null 2>&1 || true
reprepro -b "$state" --outdir "$out" includedeb trixie "$DEB"
reprepro -b "$state" --outdir "$out" list trixie
gpg --armor --export "$key" > /dist/repo/key.asc
echo "Signed with $key"
