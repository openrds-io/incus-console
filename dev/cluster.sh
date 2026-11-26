#!/usr/bin/env bash
# A small Incus cluster on a Mac, for developing Incus Console's cluster pages.
#
# Each member is a Lima virtual machine running Debian 13 with Incus from the
# Zabbly package repository. (Colima is built on Lima, but only offers its own
# Ubuntu image; Lima itself has a Debian 13 template.) The machines use Lima's
# vzNAT network, which gives each one an address the Mac and the other
# machines can reach.
#
#   dev/cluster.sh up [N]     create N members (default 3) and form the cluster
#   dev/cluster.sh token      print a trust token to add the cluster to the console
#   dev/cluster.sh status     the machines and the cluster's members
#   dev/cluster.sh shell N    a root shell on member N
#   dev/cluster.sh stop       stop the machines (start again with "up")
#   dev/cluster.sh delete     delete the machines and everything in them
#
# Settings (environment): CLUSTER_PREFIX=incus  CLUSTER_CPUS=2  CLUSTER_MEMORY=2
# CLUSTER_DISK=20 (GiB)  INCUS_CHANNEL=stable (or lts-6.0)

set -euo pipefail

PREFIX="${CLUSTER_PREFIX:-incus}"
CPUS="${CLUSTER_CPUS:-2}"
MEMORY="${CLUSTER_MEMORY:-2}"
DISK="${CLUSTER_DISK:-20}"
CHANNEL="${INCUS_CHANNEL:-stable}"
TEMPLATE="debian-13"
PORT=8443

die() { echo "cluster.sh: $*" >&2; exit 1; }
say() { echo "==> $*"; }

command -v limactl >/dev/null || die "limactl not found. Install Lima: brew install lima"

# Run a command as root on a member. Reads a script from stdin with: root NAME bash -s
root() {
  local name="$1"
  shift
  limactl shell --workdir / "$name" sudo "$@"
}

exists() { limactl list -q 2>/dev/null | grep -qx "$1"; }

state() { limactl list --format '{{.Status}}' "$1" 2>/dev/null || true; }

machines() { limactl list -q 2>/dev/null | grep -E "^${PREFIX}[0-9]+$" | sort -V || true; }

# The member's address on the vzNAT network (interface lima0).
address() {
  limactl shell --workdir / "$1" ip -4 -o addr show dev lima0 2>/dev/null |
    awk '{print $4}' | cut -d/ -f1 | head -n1
}

clustered() { root "$1" incus cluster list >/dev/null 2>&1; }

start_machine() {
  local name="$1"
  if exists "$name"; then
    if [ "$(state "$name")" != "Running" ]; then
      say "Starting $name"
      limactl start --tty=false "$name"
    fi
    return
  fi
  say "Creating $name (Debian 13, ${CPUS} CPUs, ${MEMORY}GiB memory, ${DISK}GiB disk)"
  # No folders shared with the Mac and no containerd: these are plain servers.
  # If Lima says it has no such template, update it: brew upgrade lima
  limactl start --tty=false --name "$name" --vm-type=vz --network=vzNAT \
    --cpus "$CPUS" --memory "$MEMORY" --disk "$DISK" \
    --containerd=none --set '.mounts = []' "template://$TEMPLATE"
}

install_incus() {
  local name="$1"
  say "Installing Incus ($CHANNEL, from Zabbly) on $name"
  root "$name" env CHANNEL="$CHANNEL" bash -s <<'REMOTE'
set -eu
export DEBIAN_FRONTEND=noninteractive
if ! command -v incus >/dev/null; then
  apt-get update -q
  apt-get install -y -q curl ca-certificates
  mkdir -p /etc/apt/keyrings
  curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc
  cat > "/etc/apt/sources.list.d/zabbly-incus-$CHANNEL.sources" <<SOURCES
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/$CHANNEL
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/zabbly.asc
SOURCES
  apt-get update -q
  apt-get install -y -q incus
fi
incus admin waitready --timeout 120
REMOTE
}

bootstrap() {
  local name="$1" ip="$2"
  say "Starting the cluster on $name ($ip)"
  root "$name" incus admin init --preseed <<PRESEED
config:
  core.https_address: $ip:$PORT
storage_pools:
- name: default
  driver: dir
networks:
- name: incusbr0
  type: bridge
  config:
    ipv4.address: auto
    ipv6.address: none
profiles:
- name: default
  devices:
    root:
      path: /
      pool: default
      type: disk
    eth0:
      name: eth0
      network: incusbr0
      type: nic
cluster:
  server_name: $name
  enabled: true
PRESEED
}

join() {
  local name="$1" ip="$2" first="$3" first_ip="$4" token
  # The members must reach each other, not just the Mac.
  root "$name" timeout 5 bash -c "exec 3<>/dev/tcp/$first_ip/$PORT" 2>/dev/null ||
    die "$name cannot reach $first at $first_ip:$PORT. The machines must be on the same vzNAT network."
  say "Joining $name ($ip) to the cluster"
  token="$(root "$first" incus cluster add "$name" | tr -d '\r' | awk 'NF' | tail -n1)"
  [ -n "$token" ] || die "no join token from $first"
  root "$name" incus admin init --preseed <<PRESEED
cluster:
  enabled: true
  server_address: $ip:$PORT
  cluster_token: $token
  member_config:
  - entity: storage-pool
    name: default
    key: source
    value: ""
PRESEED
}

up() {
  local count="${1:-3}" first="${PREFIX}1" first_ip name ip i
  [[ "$count" =~ ^[1-9][0-9]*$ ]] || die "up takes a number of members, such as 3"
  for i in $(seq 1 "$count"); do
    name="${PREFIX}${i}"
    start_machine "$name"
    install_incus "$name"
    ip="$(address "$name")"
    [ -n "$ip" ] || die "$name has no address on lima0 (vzNAT)"
    if clustered "$name"; then
      say "$name is already a member ($ip)"
    elif [ "$i" = 1 ]; then
      bootstrap "$name" "$ip"
    else
      join "$name" "$ip" "$first" "$first_ip"
    fi
    [ "$i" = 1 ] && first_ip="$ip"
  done
  echo
  root "$first" incus cluster list
  echo
  echo "Add the cluster to Incus Console: run 'dev/cluster.sh token' and paste the token"
  echo "under Servers > Remote, with a trust token. One member is enough; any will do."
}

token() {
  local first="${PREFIX}1"
  exists "$first" || die "no $first yet. Run: dev/cluster.sh up"
  root "$first" incus config trust add "incus-console-$(date +%s)" | tr -d '\r' | awk 'NF' | tail -n1
}

status() {
  limactl list $(machines)
  local first="${PREFIX}1"
  if exists "$first" && [ "$(state "$first")" = "Running" ]; then
    echo
    root "$first" incus cluster list || true
  fi
}

case "${1:-}" in
  up) up "${2:-}" ;;
  token) token ;;
  status) status ;;
  shell)
    [ -n "${2:-}" ] || die "shell takes a member number, such as 2"
    limactl shell --workdir / "${PREFIX}${2}" sudo -i
    ;;
  stop) for name in $(machines); do limactl stop "$name" || true; done ;;
  delete)
    for name in $(machines); do limactl delete --force "$name"; done
    ;;
  *) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
