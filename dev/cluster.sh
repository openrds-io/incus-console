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
#   dev/cluster.sh spare N    member N as a server on its own, not in the cluster:
#                             for starting a cluster or joining one from the console
#   dev/cluster.sh token [N]  print a trust token to add the cluster (or member N)
#                             to the console
#   dev/cluster.sh status     the machines and the cluster's members
#   dev/cluster.sh shell N    a root shell on member N
#   dev/cluster.sh doctor     network checks, for when members cannot reach each other
#   dev/cluster.sh stop       stop the machines (start again with "up")
#   dev/cluster.sh delete     delete the machines and everything in them
#   dev/cluster.sh base       build (or rebuild) incus-base: Debian 13 upgraded, with
#                             Incus installed, stopped. New machines are cloned from it,
#                             which skips the upgrade and the install. Rebuild it now
#                             and then for newer packages; "delete" leaves it alone.
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

# Incus writes cluster.crt when a server starts or joins a cluster. Asking
# Incus itself fails when the cluster's database cannot be reached, as on a
# member whose fellow members are stopped or gone.
clustered() { root "$1" test -e /var/lib/incus/cluster.crt; }

BASE="${PREFIX}-base"

# Lima 1.1 and later can copy a stopped machine.
can_clone() { limactl clone --help >/dev/null 2>&1; }

start_machine() {
  local name="$1"
  if exists "$name"; then
    if [ "$(state "$name")" != "Running" ]; then
      say "Starting $name"
      limactl start --tty=false "$name"
    fi
    return
  fi
  if [ "$name" != "$BASE" ] && exists "$BASE" && can_clone; then
    [ "$(state "$BASE")" = "Stopped" ] || limactl stop "$BASE"
    say "Creating $name from $BASE"
    # --tty=false: no "start it now?" question; it is started just below.
    limactl clone --tty=false "$BASE" "$name"
    limactl start --tty=false "$name"
    return
  fi
  say "Creating $name (Debian 13, ${CPUS} CPUs, ${MEMORY}GiB memory, ${DISK}GiB disk)"
  # No folders shared with the Mac and no containerd: these are plain servers.
  # If Lima says it has no such template, update it: brew upgrade lima
  limactl start --tty=false --name "$name" --vm-type=vz --network=vzNAT \
    --cpus "$CPUS" --memory "$MEMORY" --disk "$DISK" \
    --containerd=none --set '.mounts = []' "template://$TEMPLATE"
}

# Lima sends a machine's internet traffic out through vzNAT (lima0) when it
# has one. VPNs and firewalls on the Mac often block that path, while Lima's
# ordinary network (eth0) goes out through the Mac's own connections and
# works. So: eth0 for the internet, lima0 for the members and the Mac.
prefer_eth0() {
  local name="$1"
  root "$name" bash -s <<'REMOTE'
set -eu
metric=1000
file="$(networkctl status lima0 2>/dev/null | sed -n 's/.*Network File: *//p' | head -n1)"
if [ -n "$file" ]; then
  # Managed by systemd-networkd: a drop-in survives restarts and lease renewals.
  dropin="/etc/systemd/network/$(basename "$file").d"
  if [ ! -f "$dropin/50-cluster-sh.conf" ]; then
    mkdir -p "$dropin"
    printf '[DHCPv4]\nRouteMetric=%s\n\n[IPv6AcceptRA]\nRouteMetric=%s\n' "$metric" "$metric" \
      > "$dropin/50-cluster-sh.conf"
    networkctl reload
    networkctl reconfigure lima0
  fi
fi
# Whatever manages it: wait for the address, then make sure no default route
# through lima0 outranks eth0's (metric 200).
for _ in $(seq 1 30); do
  ip -4 -o addr show dev lima0 | grep -q inet && break
  sleep 1
done
ip -4 route show default dev lima0 | while read -r route; do
  case "$route" in *"metric $metric"*) continue ;; esac
  plain="$(echo "$route" | sed 's/ metric [0-9]*//')"
  # shellcheck disable=SC2086
  ip -4 route del $route || true
  # shellcheck disable=SC2086
  ip -4 route add $plain metric "$metric" 2>/dev/null || true
done
ip -6 route show default dev lima0 2>/dev/null | while read -r route; do
  case "$route" in *"metric $metric"*) continue ;; esac
  # shellcheck disable=SC2086
  ip -6 route del $route || true
done
REMOTE
  root "$name" curl -fsS -m 20 -o /dev/null https://pkgs.zabbly.com/key.asc ||
    die "$name cannot reach the internet (https://pkgs.zabbly.com). Check: limactl shell $name ip route"
}

# macOS puts every vzNAT machine on a "private" port of its bridge: each can
# talk to the Mac, but not directly to another machine. So the members reach
# each other through the Mac, which routes between them: send everything for
# the vzNAT subnet to the gateway instead of straight to the neighbour.
relay_via_mac() {
  local name="$1"
  root "$name" bash -s <<'REMOTE'
set -eu
gateway="$(ip -4 route show default dev lima0 | awk '{print $3; exit}')"
address="$(ip -4 -o addr show dev lima0 | awk '{print $4; exit}' | cut -d/ -f1)"
[ -n "$gateway" ] && [ -n "$address" ] || exit 0
prefix="${address%.*}"
# The Mac will say "talk to it directly"; that is exactly what does not work.
printf 'net.ipv4.conf.all.accept_redirects = 0\nnet.ipv4.conf.lima0.accept_redirects = 0\n' \
  > /etc/sysctl.d/60-cluster-sh.conf
sysctl -qw net.ipv4.conf.all.accept_redirects=0 net.ipv4.conf.lima0.accept_redirects=0
# Kept in systemd-networkd's settings too, when it manages lima0: plain routes
# are lost whenever lima0 is set up again (a DHCP renewal, a restart), and the
# member then cannot reach the others.
file="$(networkctl status lima0 2>/dev/null | sed -n 's/.*Network File: *//p' | head -n1)"
if [ -n "$file" ]; then
  dropin="/etc/systemd/network/$(basename "$file").d"
  mkdir -p "$dropin"
  printf '[Route]\nDestination=%s.0/25\nGateway=%s\n\n[Route]\nDestination=%s.128/25\nGateway=%s\n' \
    "$prefix" "$gateway" "$prefix" "$gateway" > "$dropin/60-cluster-sh-relay.conf"
  networkctl reload
  networkctl reconfigure lima0
  for _ in $(seq 1 30); do
    ip -4 route show default dev lima0 | grep -q . && break
    sleep 1
  done
fi
ip -4 route replace "$gateway/32" dev lima0 scope link
ip -4 route replace "$prefix.0/25" via "$gateway" dev lima0
ip -4 route replace "$prefix.128/25" via "$gateway" dev lima0
REMOTE
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
    die "$name cannot reach $first at $first_ip:$PORT through the Mac. Run: dev/cluster.sh doctor"
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

need_forwarding() {
  if [ "$(sysctl -n net.inet.ip.forwarding 2>/dev/null)" != "1" ]; then
    # See relay_via_mac. Not done here: it needs sudo and is yours to decide.
    die "the members reach each other through the Mac, which is not forwarding packets.
Turn it on (until the next restart of the Mac) with:
    sudo sysctl -w net.inet.ip.forwarding=1"
  fi
}

# A running machine with Incus installed; prints nothing.
prepare() {
  local name="$1"
  start_machine "$name"
  prefer_eth0 "$name"
  relay_via_mac "$name"
  install_incus "$name"
}

# A machine to clone the others from: everything up to Incus being installed,
# and nothing of its own that its clones must not share.
base() {
  if exists "$BASE"; then
    say "Replacing $BASE"
    limactl delete --force "$BASE"
  fi
  can_clone || die "this Lima cannot clone machines (limactl clone); update it: brew upgrade lima"
  prepare "$BASE"
  say "Upgrading Debian on $BASE"
  root "$BASE" bash -s <<'REMOTE'
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get full-upgrade -y -q
apt-get autoremove -y -q
apt-get clean
# Incus makes its certificate and database on its first start: each clone its own.
systemctl stop 'incus*' 2>/dev/null || true
rm -rf /var/lib/incus/* /var/log/incus/*
# A clone with the same machine-id would get the same address from DHCP.
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
REMOTE
  limactl stop "$BASE"
  echo
  echo "$BASE is ready. New machines (up, spare) are now cloned from it."
}

up() {
  local count="${1:-3}" first="${PREFIX}1" first_ip name ip i
  [[ "$count" =~ ^[1-9][0-9]*$ ]] || die "up takes a number of members, such as 3"
  if [ "$count" -gt 1 ]; then need_forwarding; fi
  for i in $(seq 1 "$count"); do
    name="${PREFIX}${i}"
    prepare "$name"
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

# Listening on the network and nothing else: no pool, no network, no cluster.
# That is what a server joining a cluster must look like, and starting a
# cluster from the console also works from here.
spare() {
  local n="${1:-}" name ip
  [[ "$n" =~ ^[1-9][0-9]*$ ]] || die "spare takes a member number, such as 4"
  need_forwarding
  name="${PREFIX}${n}"
  prepare "$name"
  ip="$(address "$name")"
  [ -n "$ip" ] || die "$name has no address on lima0 (vzNAT)"
  if clustered "$name"; then
    die "$name is already a member of a cluster ($ip). To start over, delete it:
    limactl delete --force $name
(or 'dev/cluster.sh delete' for all of them), then run this again."
  fi
  root "$name" incus config set core.https_address="$ip:$PORT"
  echo
  echo "$name stands alone at $ip:$PORT. Add it to Incus Console under Servers > Remote,"
  echo "with this trust token; then start a cluster on it, or join it to one:"
  echo
  token "$n"
}

token() {
  local member="${PREFIX}${1:-1}"
  exists "$member" || die "no $member yet. Run: dev/cluster.sh up (or spare ${1:-1})"
  root "$member" incus config trust add "incus-console-$(date +%s)" | tr -d '\r' | awk 'NF' | tail -n1
}

status() {
  limactl list $(machines)
  local first="${PREFIX}1"
  if exists "$first" && [ "$(state "$first")" = "Running" ]; then
    echo
    root "$first" incus cluster list || true
  fi
}

# Everything here reads nothing from the terminal, so the whole report comes
# out even when the command was pasted along with others.
doctor() {
  local name other ip
  for name in $(machines); do
    echo "===== $name"
    [ "$(state "$name")" = "Running" ] || { echo "not running"; continue; }
    ip="$(address "$name" </dev/null)"
    echo "lima0 address: ${ip:-none}"
    limactl shell --workdir / "$name" ip -4 route </dev/null || true
    limactl shell --workdir / "$name" ip -br link </dev/null || true
    echo "--- listening"
    root "$name" ss -ltn </dev/null 2>&1 | grep -E ':8443|State' || echo "nothing on $PORT"
    echo "--- gateway"
    limactl shell --workdir / "$name" ping -c 1 -W 2 192.168.64.1 </dev/null 2>&1 | tail -n 2 || true
    for other in $(machines); do
      [ "$other" = "$name" ] && continue
      ip="$(address "$other" </dev/null)"
      [ -n "$ip" ] || continue
      echo "--- $name -> $other ($ip)"
      limactl shell --workdir / "$name" ping -c 1 -W 2 "$ip" </dev/null 2>&1 | tail -n 2 || true
      root "$name" timeout 3 bash -c "exec 3<>/dev/tcp/$ip/$PORT" </dev/null 2>&1 &&
        echo "port $PORT: open" || echo "port $PORT: no answer"
    done
    echo "--- neighbours"
    limactl shell --workdir / "$name" ip neigh show dev lima0 </dev/null || true
    echo "--- Mac -> $name"
    ip="$(address "$name" </dev/null)"
    if [ -n "$ip" ]; then
      ping -c 1 -t 2 "$ip" 2>&1 | tail -n 2 || true
      nc -vz -G 3 "$ip" "$PORT" 2>&1 || true
    fi
  done
  echo "===== Mac"
  ifconfig 2>/dev/null | grep -E '^(bridge|vmenet)[0-9]+:|inet 192\.168\.64\.|member:' || true
  sysctl net.inet.ip.forwarding 2>/dev/null || true
  limactl --version
  sw_vers 2>/dev/null || true
}

case "${1:-}" in
  up) up "${2:-}" ;;
  spare) spare "${2:-}" ;;
  token) token "${2:-}" ;;
  base) base ;;
  status) status ;;
  doctor) doctor ;;
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
