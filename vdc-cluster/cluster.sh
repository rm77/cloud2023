#!/usr/bin/env bash
# cluster.sh - lab PC side of vdc-cluster (run as root)
#
#   ./cluster.sh init                    SSH key + join token in state/; resets br-under, tap-hub, taps
#   ./cluster.sh build [hub|spoke]       Packer -> images/*.qcow2, with progress
#   ./cluster.sh up                      br-under + hub VM (main PC), follows its boot
#   ./cluster.sh spoke add [COUNT]       clone the spoke image, boot in parallel, follow the join
#   ./cluster.sh list                    VMs on this PC + cluster view from the hub
#   ./cluster.sh start|stop NAME|all     (start follows boot + join)
#   ./cluster.sh rm NAME                 evict from the hub, stop, delete the clone
#   ./cluster.sh console NAME            serial console (Ctrl-] to leave)
#   ./cluster.sh ssh NAME [CMD...]       any node, also ones on other PCs
#   ./cluster.sh hubctl ARGS...          run hubctl on the hub
#   ./cluster.sh image add NAME FILE|URL [--type cloud-init|cirros|none] [--mem MB] [--disk SIZE] [--user LOGIN]
#                                        tenant base image -> the hub's catalog (FILE: uploaded from this PC)
#   ./cluster.sh images                  the catalog
#   ./cluster.sh capture NAME [FILTER]   tcpdump on a local VM's underlay tap
#   ./cluster.sh logs                    list everything in logs/
#   ./cluster.sh logs tail NAME          follow a VM's serial console log
#   ./cluster.sh logs collect [NAME|all] copy /var/log/lab etc. from nodes into logs/nodes/
#   ./cluster.sh logs commands [TOOL]    command journal: per-tool counts, or every TOOL call
#   ./cluster.sh update [all|hub|spokes] copy changed lab scripts into the running VMs (no rebuild)
#   ./cluster.sh edge up|down|status|sync  main PC: campus ports to the hub and guests, tenant routes
#
# cloud API (cloud/, main PC): NIST CCRA REST API for providers, tenants and auditors
#   ./cluster.sh cloud install [--dev]   own Python environment, Redis, cloudflared, secrets in state/cloud/
#   ./cluster.sh cloud start|stop|restart|status
#   ./cluster.sh cloud logs [redis|api|worker|meter]
#   ./cluster.sh cloud admin             the provider login
#   ./cluster.sh cloud enable|disable    run it under systemd (survives reboots)
#   ./cluster.sh cloud test              the API's test suite (stand-in hub, no cluster needed)
#
# multi-PC:
#   ./cluster.sh host init ID [NIC]      this PC's identity -> host.conf (ID 1 = main PC)
#   ./cluster.sh host show
#   ./cluster.sh host export [FILE]      bundle for another PC: kit + spoke image + token + key
#   ./cluster.sh mtu-check [IP]          do UNDERLAY_MTU frames reach IP (default: the hub)?
#   ./cluster.sh net-up | net-down       just the underlay bridge (+ uplink NIC)
#
# Everything this script prints is also appended to logs/cluster.log; every provisioning
# tool it runs (qemu, qemu-img, ip, packer, ...) is journaled in logs/commands/<tool>.log.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
KIT=$PWD
# temporary files stay in the kit folder too (state/tmp, git-ignored)
mkdir -p "$KIT/state/tmp" 2>/dev/null && export TMPDIR=$KIT/state/tmp
# shellcheck source=cluster.conf
. ./cluster.conf
HOST_ID=1 HOST_ROLE=main UPLINK_NIC=""
# shellcheck disable=SC1091
if [ -f host.conf ]; then . ./host.conf; fi
ST=$KIT/state
VMS=$ST/vms
KEY=$ST/id_lab
LOGS=$KIT/logs
: "${JOIN_TIMEOUT:=300}" "${IP_MODE:=static}"
NET_BASE=${UNDERLAY_NET%.*.*}          # 10.20

die() { echo "error: $*" >&2; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || die "run as root: sudo $0 ${ARGS[*]}"; }
# -F /dev/null: ignore ~/.ssh/config (and leave ~/.ssh alone): only the lab key, no known_hosts
SSH_OPTS=(-F /dev/null -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o ConnectTimeout=5)
lab_ssh()   { local ip=$1; shift; ssh "${SSH_OPTS[@]}" "root@$ip" "$@"; }
lab_ssh_t() { local ip=$1; shift; ssh -t "${SSH_OPTS[@]}" "root@$ip" "$@"; }
hub() { lab_ssh "$HUB_IP" "$@"; }
pid_of() { local p=$VMS/$1.pid; [ -f "$p" ] && kill -0 "$(cat "$p")" 2>/dev/null && cat "$p"; }
running() { pid_of "$1" >/dev/null; }
vm_names() { local f; for f in "$VMS"/*.env; do [ -e "$f" ] && basename "$f" .env; done | sort -V; }
vm_role() { sed -n 's/^ROLE=//p' "$VMS/$1.env"; }
elapsed() { printf '%d:%02d' $(($1 / 60)) $(($1 % 60)); }
step() { LAB_STEP=$*; export LAB_STEP; }     # "why" of the next commands, in the journal
spoke_name() { printf 'hv%d%02d' "$HOST_ID" "$1"; }
spoke_mac() { printf '52:54:00:21:%02x:%02x' "$HOST_ID" "$1"; }
spoke_ip() { echo "$NET_BASE.$HOST_ID.$((100 + $1))"; }   # hv203 = 10.20.2.103
host_ip() { echo "$NET_BASE.$1.1"; }

# mirror this run's output into logs/cluster.log (timestamped)
start_log() {
  mkdir -p "$LOGS/vm" "$LOGS/build" "$LOGS/nodes"
  printf '\n%s ==== cluster.sh %s  (host %s, %s) ====\n' "$(date '+%F %T')" "$*" "$HOST_ID" "$HOST_ROLE" \
    >>"$LOGS/cluster.log"
  exec > >(while IFS= read -r l; do
             printf '%s\n' "$l"
             printf '%s %s\n' "$(date +%T)" "$l" >>"$LOGS/cluster.log"
           done) 2>&1
  LOGPID=$!
  trap 'exec >&- 2>&-; wait "$LOGPID" 2>/dev/null || true' EXIT
}

# command journal: provisioning tools run through lab-trace (logs/commands/<tool>.log)
trace_setup() {   # CONTEXT
  : "${TRACE:=changes}" "${TRACE_OUTPUT:=no}"
  export TRACE TRACE_OUTPUT
  [ "$TRACE" != off ] || return 0
  "$KIT/common/rootfs/usr/local/lib/lab/lab-trace" --install "$ST/trace.d" >/dev/null
  PATH=$ST/trace.d:$PATH LAB_TRACE_DIR=$LOGS/commands LAB_TRACE_HOST=pc$HOST_ID LAB_TRACE_CTX="cluster.sh $*"
  export PATH LAB_TRACE_DIR LAB_TRACE_HOST LAB_TRACE_CTX
}

check_host() {
  [[ $HOST_ID =~ ^[0-9]+$ ]] && [ "$HOST_ID" -ge 1 ] && [ "$HOST_ID" -le 254 ] || die "HOST_ID must be 1..254 (host.conf)"
  case $HOST_ROLE in main|worker) ;; *) die "HOST_ROLE must be main or worker (host.conf)" ;; esac
  case $IP_MODE in static|dhcp) ;; *) die "IP_MODE must be static or dhcp (cluster.conf)" ;; esac
  if [ "$IP_MODE" = static ] && [ "$UNDERLAY_PREFIX" != 16 ]; then
    die "IP_MODE=static uses the 10.x.<host>.<spoke> plan and needs UNDERLAY_PREFIX=16"
  fi
}

# ---------------------------------------------------------------- underlay
# With br_netfilter loaded (Docker, ufw, libvirt on the lab PC) frames switched between
# two ports of br-under also pass the iptables FORWARD chain, and Docker's DROP policy
# silently kills VM-to-VM traffic while lab PC <-> VM still works. Allow the underlay.
bridge_fw_allow() {
  local t
  [ "$(cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null || echo 0)" = 1 ] || return 0
  for t in iptables ip6tables; do
    command -v "$t" >/dev/null 2>&1 || continue
    "$t" -C FORWARD -i "$UNDERLAY_BR" -o "$UNDERLAY_BR" -j ACCEPT 2>/dev/null && continue
    step "host firewall: let frames between VMs on $UNDERLAY_BR through ($t FORWARD)"
    "$t" -I FORWARD 1 -i "$UNDERLAY_BR" -o "$UNDERLAY_BR" -j ACCEPT -m comment --comment vdc-cluster ||
      echo "warning: could not add the $t FORWARD rule for $UNDERLAY_BR: VMs may not reach each other"
  done
}
bridge_fw_remove() {
  local t
  for t in iptables ip6tables; do
    command -v "$t" >/dev/null 2>&1 || continue
    while "$t" -D FORWARD -i "$UNDERLAY_BR" -o "$UNDERLAY_BR" -j ACCEPT -m comment --comment vdc-cluster 2>/dev/null; do :; done
  done
}

# (re)initialise tap-NAME: a stopped VM's tap is deleted and created fresh (no stale
# master/MTU/state); a running VM keeps its tap (QEMU holds it) but is re-attached.
tap_init() {   # NAME
  local n=$1 t=tap-$1
  if running "$n"; then
    if ! ip link show "$t" >/dev/null 2>&1; then
      echo "  $n  warning: running but $t is gone: restart it (./cluster.sh stop $n; ./cluster.sh start $n)"; return 0
    fi
    step "$t: re-attach running $n to $UNDERLAY_BR"
  else
    step "$t: fresh underlay port for $n on $UNDERLAY_BR"
    ip link del "$t" 2>/dev/null || true
    ip tuntap add dev "$t" mode tap
  fi
  ip link set "$t" mtu "$UNDERLAY_MTU" master "$UNDERLAY_BR" up
  echo "  $t  mtu $UNDERLAY_MTU, port of $UNDERLAY_BR$(running "$n" && echo " ($n running)")"
}

# the underlay's own route: the kernel drops it when an address on br-under is removed, and
# without it nothing can be routed via the hub
underlay_route() {
  ip route replace "$UNDERLAY_NET/$UNDERLAY_PREFIX" dev "$UNDERLAY_BR" proto kernel scope link src "$LABPC_IP"
}

cmd_net_up() {
  step "underlay switch: Linux bridge $UNDERLAY_BR with this PC's address"
  ip link show "$UNDERLAY_BR" >/dev/null 2>&1 || ip link add "$UNDERLAY_BR" type bridge
  ip link set "$UNDERLAY_BR" mtu "$UNDERLAY_MTU" up
  ip addr replace "$LABPC_IP/$UNDERLAY_PREFIX" dev "$UNDERLAY_BR"
  underlay_route
  if [ -n "$UPLINK_NIC" ]; then
    local parent=${UPLINK_NIC%%.*} vid
    step "uplink $UPLINK_NIC into $UNDERLAY_BR: joins the other PCs' underlay"
    ip link show "$parent" >/dev/null 2>&1 || die "UPLINK_NIC: no interface $parent"
    if [ "$parent" != "$UPLINK_NIC" ] && ! ip link show "$UPLINK_NIC" >/dev/null 2>&1; then
      vid=${UPLINK_NIC#*.}
      [[ $vid =~ ^[0-9]+$ ]] || die "UPLINK_NIC: VLAN id in $UPLINK_NIC must be a number"
      ip link add link "$parent" name "$UPLINK_NIC" type vlan id "$vid"
    fi
    if [ -n "$(ip -4 -o addr show dev "$UPLINK_NIC")" ]; then
      die "$UPLINK_NIC has an IPv4 address: use a dedicated NIC or a VLAN (UPLINK_NIC=$parent.<vid>)"
    fi
    ip link set "$parent" mtu "$UNDERLAY_MTU" up 2>/dev/null ||
      echo "warning: $parent refuses MTU $UNDERLAY_MTU (see mtu-check)"
    ip link set "$UPLINK_NIC" mtu "$UNDERLAY_MTU" up 2>/dev/null || true
    ip link set "$UPLINK_NIC" master "$UNDERLAY_BR"
    echo "uplink $UPLINK_NIC bridged into $UNDERLAY_BR"
  fi
  bridge_fw_allow
  echo "$UNDERLAY_BR up: $LABPC_IP/$UNDERLAY_PREFIX mtu $UNDERLAY_MTU"
  local other
  other=$(ip -4 -o addr show dev "$UNDERLAY_BR" 2>/dev/null | awk -v me="$LABPC_IP" '{split($4, a, "/"); if (a[1] != me) printf "%s ", $4}')
  if [ -n "$other" ]; then
    echo "warning: $UNDERLAY_BR also has $other- the hub only trusts $LABPC_IP; remove with: ip addr del <addr> dev $UNDERLAY_BR" >&2
  fi
}

cmd_net_down() {
  step "remove the underlay bridge"
  if [ -n "$UPLINK_NIC" ] && [ "${UPLINK_NIC%%.*}" != "$UPLINK_NIC" ]; then ip link del "$UPLINK_NIC" 2>/dev/null || true; fi
  ip link del "$UNDERLAY_BR" 2>/dev/null || true
  bridge_fw_remove
  edge_clear
  ip route flush proto "$EDGE_RT_PROTO" 2>/dev/null || true
  edge_forward_restore
  echo "$UNDERLAY_BR removed"
}

cmd_mtu_check() {
  local t=${1:-$HUB_IP} s=$((UNDERLAY_MTU - 28))
  step "MTU probe: $UNDERLAY_MTU-byte frames, don't fragment"
  if ! ping -c 1 -W 2 "$t" >/dev/null 2>&1; then
    echo "mtu-check: $t does not answer ping at all (cabling, UPLINK_NIC, VLAN, is it up?)"; return 1
  fi
  if ping -c 2 -W 2 -M "do" -s "$s" "$t" >/dev/null 2>&1; then
    echo "mtu-check: $UNDERLAY_MTU-byte frames reach $t: ok"; return 0
  fi
  echo "mtu-check: small pings reach $t, $UNDERLAY_MTU-byte frames are dropped."
  echo "  The switch or a NIC on the path does not pass frames > 1500. Either enable jumbo"
  echo "  frames there, or set UNDERLAY_MTU=1500 TENANT_MTU=1450 in cluster.conf on EVERY PC"
  echo "  and rebuild the images (./cluster.sh build)."
  return 1
}

# ---------------------------------------------------------------- VMs
vm_create() {   # NAME ROLE MAC MEM CPUS [IP]
  local n=$1 role=$2 base
  base=$(readlink -f "images/$role.qcow2")
  [ -f "$base" ] || die "images/$role.qcow2 missing (./cluster.sh build $role, or host export from the main PC)"
  [ -s "$ST/token" ] || die "state/token missing (main PC: ./cluster.sh init, other PCs: host bundle)"
  mkdir -p "$VMS"
  step "disk of $n: $CLONE clone of images/$role.qcow2"
  if [ "$CLONE" = full ]; then
    cp --sparse=always "$base" "$VMS/$n.qcow2"
  else
    qemu-img create -q -f qcow2 -F qcow2 -b "$base" "$VMS/$n.qcow2"
  fi
  printf 'ROLE=%s\nMAC=%s\nMEM=%s\nCPUS=%s\nUUID=%s\nHOST_ID=%s\nIP=%s\n' \
    "$role" "$3" "$4" "$5" "$(cat /proc/sys/kernel/random/uuid)" "$HOST_ID" "${6:-}" >"$VMS/$n.env"
  echo "  $n  disk   $CLONE clone of images/$role.qcow2"
}

declare -A OFF=()   # console log size before boot: only newer lines count as progress
mark_offset() { local f=$LOGS/vm/$1.console.log; OFF[$1]=$( [ -f "$f" ] && stat -c %s "$f" || echo 0); }

vm_start() {   # NAME
  local n=$1 ROLE MAC MEM CPUS UUID HOST_ID IP="" qlog
  if running "$n"; then echo "  $n  already running"; return 1; fi
  [ -f "$VMS/$n.env" ] || die "no VM $n"
  # shellcheck disable=SC1090
  . "$VMS/$n.env"
  mkdir -p "$LOGS/vm"
  qlog=$LOGS/vm/$n.qemu.log
  mark_offset "$n"
  ip link show "$UNDERLAY_BR" >/dev/null 2>&1 || cmd_net_up
  tap_init "$n" >/dev/null
  local accel=(-machine "q35,accel=kvm" -cpu host)
  [ -w /dev/kvm ] || { accel=(-machine "q35,accel=tcg" -cpu max); echo "  $n  warning: no KVM on this PC, runs on TCG (slow)"; }
  local nics=()
  if [ "$ROLE" = hub ]; then   # eth0 = uplink (user-net), eth1 = underlay
    nics+=(-netdev "user,id=up0" -device "virtio-net-pci,netdev=up0")
  fi
  nics+=(-netdev "tap,id=u0,ifname=tap-$n,script=no,downscript=no"
         -device "virtio-net-pci,netdev=u0,mac=$MAC,host_mtu=$UNDERLAY_MTU")
  local addr=()   # static mode: the spoke's lab-net reads its address from here
  if [ "$ROLE" = spoke ] && [ "$IP_MODE" = static ] && [ -n "$IP" ]; then
    addr=(-smbios "type=11,value=lab.ip=$IP/$UNDERLAY_PREFIX")
  fi
  printf '%s starting %s\n' "$(date '+%F %T')" "$n" >>"$qlog"
  step "boot $n ($ROLE): CPU, RAM, disk, NICs, SMBIOS identity, serial console"
  if ! qemu-system-x86_64 -name "$n" -uuid "$UUID" "${accel[@]}" -smp "$CPUS" -m "$MEM" \
      -drive "file=$VMS/$n.qcow2,if=virtio,format=qcow2" "${nics[@]}" \
      -smbios "type=11,value=lab.hub=$HUB_IP" \
      -smbios "type=11,value=lab.token=$(cat "$ST/token")" \
      -smbios "type=11,value=lab.name=$n" "${addr[@]}" \
      -chardev "socket,id=ser0,path=$VMS/$n.serial,server=on,wait=off,logfile=$LOGS/vm/$n.console.log,logappend=on" \
      -serial chardev:ser0 -display none \
      -monitor "unix:$VMS/$n.mon,server=on,wait=off" \
      -daemonize -pidfile "$VMS/$n.pid" 2>>"$qlog"; then
    tail -n 5 "$qlog" | sed 's/^/      | /'
    die "QEMU failed to start $n (log: $qlog)"
  fi
  echo "  $n  start  QEMU pid $(cat "$VMS/$n.pid"), tap-$n, $MAC, ${IP:-DHCP}"
}

vm_stop() {   # NAME
  local n=$1 i
  if ! running "$n"; then echo "  $n  not running"; ip link del "tap-$n" 2>/dev/null || true; return; fi
  step "power off $n: ACPI button via the QEMU monitor"
  echo system_powerdown | socat - "UNIX-CONNECT:$VMS/$n.mon" >/dev/null 2>&1 || true
  for i in $(seq 60); do running "$n" || break; sleep 1; done
  if running "$n"; then kill "$(pid_of "$n")"; sleep 1; echo "  $n  did not power off, killed"; fi
  step "remove tap-$n"
  ip link del "tap-$n" 2>/dev/null || true
  echo "  $n  stopped"
}

# ---------------------------------------------------------------- progress
# Boot/join stages, recognised in the serial console log: "regex@@label" (\1 = detail).
# The VMs print "LAB: ..." lines (lab-agent, hubctl, lab-api) to their console for this.
SPOKE_STAGES=(
  'is starting up@@booting (OpenRC)'
  'LAB: address (.*)@@underlay \1'
  'LAB: agent started@@agent started'
  'LAB: joining hub ([0-9.]+)@@join request -> hub \1'
  'LAB: joined as ([a-z0-9]+ \([0-9.]+\))@@joined as \1'
  'LAB: ready as (.*)@@tunnels + libvirt networks up: \1'
)
HUB_STAGES=(
  'is starting up@@booting (OpenRC)'
  'LAB: hub state ready(.*)@@hub state ready\1'
  'LAB: hub API listening on ([0-9.:]+)@@join API listening on \1'
)
declare -A T0=() TEND=() CUR=() RESULT=() NODE=() WARNED=()

console_text() {   # NAME -> console output since OFF[NAME], without CR and colour codes
  local f=$LOGS/vm/$1.console.log
  [ -f "$f" ] || return 0
  tail -c +"$(( ${OFF[$1]:-0} + 1 ))" "$f" | tr -d '\r' | sed -E 's/\x1b\[[0-9;?]*[A-Za-z]//g'
}

progress() {   # NAME STEP TOTAL TEXT
  local bar
  bar=$(printf '%*s' "$2" '' | tr ' ' '#')$(printf '%*s' $(($3 - $2)) '' | tr ' ' '.')
  printf '  %5s  %-7s [%s] %d/%d  %s\n' "$(elapsed $((SECONDS - T0[$1])))" "$1" "$bar" "$2" "$3" "$4"
}

finish() { RESULT[$1]=$2; TEND[$1]=$SECONDS; }

watch_boot() {   # NAME...  follow boot + join of these VMs until Ready or JOIN_TIMEOUT
  local n role text i hi re label line total pending view viewed rc=0
  local -a stages
  for n in "$@"; do T0[$n]=$SECONDS; CUR[$n]=0; RESULT[$n]=""; done
  echo "following $* (serial logs: logs/vm/<name>.console.log; Ctrl-C only stops watching)"
  while :; do
    pending=0 viewed=0 view=""
    for n in "$@"; do
      [ -z "${RESULT[$n]}" ] || continue
      pending=1
      role=$(vm_role "$n")
      if [ "$role" = hub ]; then stages=("${HUB_STAGES[@]}"); else stages=("${SPOKE_STAGES[@]}"); fi
      total=$(( ${#stages[@]} + 1 ))
      text=$(console_text "$n")
      # highest stage reached; stages whose marker was missed are filled in
      hi=-1
      for i in "${!stages[@]}"; do re=${stages[$i]%%@@*}; if grep -qE "$re" <<<"$text"; then hi=$i; fi; done
      while [ "${CUR[$n]}" -le "$hi" ]; do
        i=${CUR[$n]}; re=${stages[$i]%%@@*}; label=${stages[$i]#*@@}
        line=$(sed -nE "s@.*${re}.*@${label}@p" <<<"$text" | tail -n 1)
        [ -n "$line" ] || line=${label//\\1/}
        progress "$n" $((i + 1)) "$total" "$line"
        CUR[$n]=$((i + 1))
      done
      # problems the VM reports, each once
      while IFS= read -r line; do
        [ -n "$line" ] && [ -z "${WARNED[$n/$line]:-}" ] || continue
        WARNED[$n/$line]=1
        printf '  %5s  %-7s  !  %s\n' "$(elapsed $((SECONDS - T0[$n])))" "$n" "$line"
      done < <(sed -nE 's/.*LAB: (.*(failed|refused|unreachable|waiting|no longer|not configured).*)/\1/p' <<<"$text")
      # last stage: confirmed by the hub
      if [ "${CUR[$n]}" -eq "${#stages[@]}" ]; then
        if [ "$role" = hub ]; then
          if hub hubctl nodes >/dev/null 2>&1; then
            progress "$n" "$total" "$total" "hubctl answers over SSH"; NODE[$n]=hub; finish "$n" ok
          fi
        else
          NODE[$n]=$(sed -nE 's/.*LAB: ready as ([a-z0-9]+).*/\1/p' <<<"$text" | tail -n 1)
          if [ "$viewed" = 0 ]; then view=$(hub hubctl nodes 2>/dev/null || true); viewed=1; fi
          if [ -n "${NODE[$n]}" ] && grep -Eq "^${NODE[$n]} .* Ready " <<<"$view"; then
            progress "$n" "$total" "$total" "Ready in hubctl nodes"; finish "$n" ok
          fi
        fi
      fi
      if [ -z "${RESULT[$n]}" ] && [ $((SECONDS - T0[$n])) -ge "$JOIN_TIMEOUT" ]; then
        finish "$n" timeout; rc=1
        echo "  $n: no further progress after ${JOIN_TIMEOUT}s. Last console lines:"
        console_text "$n" | grep -v '^[[:space:]]*$' | tail -n 8 | sed 's/^/      | /'
        echo "      full log: logs/vm/$n.console.log   inside: /var/log/lab/ (./cluster.sh logs collect $n)"
      fi
    done
    [ "$pending" = 1 ] || break
    sleep 1
  done
  echo
  printf '  %-8s %-8s %-8s %s\n' VM NODE RESULT TIME
  for n in "$@"; do
    printf '  %-8s %-8s %-8s %s\n' "$n" "${NODE[$n]:--}" "${RESULT[$n]}" "$(elapsed $((TEND[$n] - T0[$n])))"
  done
  return $rc
}

# ---------------------------------------------------------------- commands
cmd_init() {
  [ "$HOST_ROLE" = main ] || die "init runs on the main PC; this PC gets state/ from its host bundle"
  mkdir -p "$VMS" "$LOGS"
  step "lab SSH key (baked into the images)"
  if [ ! -f "$KEY" ]; then
    ssh-keygen -q -t ed25519 -N '' -C vdc-lab -f "$KEY"
    if ls "$KIT"/images/*.qcow2 >/dev/null 2>&1; then
      echo "WARNING: a NEW lab key was created, but images/ already holds images built with the old key."
      echo "         Those images will refuse SSH. Run: ./cluster.sh rm <each VM>; ./cluster.sh build; ./cluster.sh up"
    fi
  fi
  [ -s "$ST/token" ] || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$ST/token"
  chmod 600 "$ST/token"
  echo "state/ ready (key: state/id_lab, join token: $(cat "$ST/token")), logs in logs/"
  # underlay of this PC: bridge, host-firewall rule, tap-hub and the taps of local VMs
  cmd_net_up
  local n
  for n in $( (echo hub; vm_names) | sort -u); do tap_init "$n"; done
}

hub_reachable() { hub true >/dev/null 2>&1; }

# why SSH to the hub fails: ping / port 22 / ssh's own error / key fingerprint
hub_diag() {
  echo "diagnosis for $HUB_IP:"
  if ping -c1 -W2 "$HUB_IP" >/dev/null 2>&1; then echo "  ping       ok"; else echo "  ping       FAILED (underlay/bridge problem)"; fi
  if timeout 3 bash -c ">/dev/tcp/$HUB_IP/22" 2>/dev/null; then echo "  tcp/22     open"
  else echo "  tcp/22     closed - sshd not running in the hub; see the console: tail -40 logs/vm/hub.console.log"; fi
  echo "  ssh error  $(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=5 -o BatchMode=yes "root@$HUB_IP" true 2>&1 | grep -v '^Warning' | tail -n 1)"
  echo "  lab key    $(ssh-keygen -lf "$KEY.pub" 2>/dev/null | awk '{print $2}') (state/id_lab.pub, $(date -r "$KEY.pub" '+%F %T'))"
  [ -f "$KIT/images/hub.qcow2" ] && echo "  hub image  built $(date -r "$KIT/images/hub.qcow2" '+%F %T')"
  [ -f "$KIT/images/hub.qcow2" ] && [ "$KEY.pub" -nt "$KIT/images/hub.qcow2" ] &&
    echo "  >> the key is NEWER than the image: the image trusts an old key. Rebuild: ./cluster.sh rm hub; ./cluster.sh build; ./cluster.sh up"
  return 0
}

preflight_hub() {
  if [ "$HOST_ROLE" = main ] && ! running hub; then die "the hub is not running: ./cluster.sh up"; fi
  ip link show "$UNDERLAY_BR" >/dev/null 2>&1 || cmd_net_up
  bridge_fw_allow              # Docker may have started (and reset FORWARD) since net-up
  if ! hub_reachable; then
    if [ "$HOST_ROLE" = main ]; then hub_diag; die "hub VM runs but does not answer SSH (see diagnosis above)"; fi
    die "cannot reach the hub ($HUB_IP) from host $HOST_ID: main PC up? UPLINK_NIC=${UPLINK_NIC:-<none>} on the same switch/VLAN? try ./cluster.sh mtu-check"
  fi
  echo "hub $HUB_IP reachable"
  if [ "$HOST_ROLE" = worker ] && [ "${FORCE:-0}" != 1 ]; then
    cmd_mtu_check "$HUB_IP" || die "fix the MTU first (or FORCE=1 to try anyway)"
  fi
}

cmd_up() {
  [ "$HOST_ROLE" = main ] || die "the hub runs on the main PC; this is host $HOST_ID ($HOST_ROLE)"
  cmd_net_up
  if running hub; then
    echo "hub already running"; hub hubctl nodes || true
  else
    echo "== hub"
    [ -f "$VMS/hub.env" ] || vm_create hub hub 52:54:00:20:00:02 "$HUB_MEM" "$HUB_CPUS"
    vm_start hub
    watch_boot hub
    echo; hub hubctl nodes
  fi
  # routes to the tenant subnets (staff access, Cloudflare tunnels) even without the campus forward
  if [ "${EDGE:-yes}" = yes ]; then echo; cmd_edge up; else edge_routes; fi
}

cmd_spoke_add() {
  local count=${1:-1} i id n ip new=()
  [[ $count =~ ^[0-9]+$ ]] && [ "$count" -ge 1 ] || die "COUNT must be a number >= 1"
  preflight_hub
  echo "== creating $count spoke(s) on host $HOST_ID"
  for ((i = 0; i < count; i++)); do
    id=1; while [ -e "$VMS/$(spoke_name "$id").env" ]; do id=$((id + 1)); done
    [ "$id" -le 99 ] || die "host $HOST_ID already has 99 spokes"
    n=$(spoke_name "$id")
    ip=""; if [ "$IP_MODE" = static ]; then ip=$(spoke_ip "$id"); fi
    vm_create "$n" spoke "$(spoke_mac "$id")" "$SPOKE_MEM" "$SPOKE_CPUS" "$ip"
    vm_start "$n"
    new+=("$n")
  done
  echo "== boot + join"
  local rc=0
  watch_boot "${new[@]}" || rc=$?
  echo; hub hubctl nodes || true
  return $rc
}

cmd_start() {   # NAME|all
  local n names hubs=() spokes=()
  if [ "$1" = all ]; then names=$(vm_names); else names=$1; fi
  for n in $names; do [ "$(vm_role "$n")" = hub ] && hubs+=("$n") || spokes+=("$n"); done
  if [ ${#hubs[@]} -gt 0 ]; then
    if vm_start hub; then watch_boot hub; fi
  fi
  local started=()
  for n in "${spokes[@]}"; do if vm_start "$n"; then started+=("$n"); fi; done
  [ ${#started[@]} -gt 0 ] || return 0
  if hub_reachable; then watch_boot "${started[@]}"
  else echo "hub not reachable: not following the join (./cluster.sh logs tail ${started[0]})"; fi
}

cmd_rm() {
  local n=$1 g
  [ -f "$VMS/$n.env" ] || die "no VM $n on this PC"
  if [ "$n" != hub ] && hub_reachable; then
    g=$(hub hubctl nodes | awk -v n="$n" '$1==n {print $5}')
    [ "${g:-0}" -eq 0 ] || die "$n still runs $g guest(s): ./cluster.sh hubctl vms"
  fi
  vm_stop "$n"                 # stop first, so the agent cannot re-join meanwhile
  if [ "$n" != hub ] && hub_reachable; then hub hubctl node del "$n" >/dev/null 2>&1 || true; fi
  rm -f "$VMS/$n".{qcow2,env,pid,serial,mon}
  echo "  $n  deleted (its logs stay in logs/vm/)"
}

cmd_list() {
  echo "host $HOST_ID ($HOST_ROLE), underlay $LABPC_IP, uplink ${UPLINK_NIC:-none}, addressing $IP_MODE"
  printf '%-8s %-6s %-18s %-14s %s\n' VM ROLE MAC IP STATE
  local n
  for n in $(vm_names); do
    # shellcheck disable=SC1090
    (IP=""; . "$VMS/$n.env"; [ "$ROLE" = hub ] && IP=$HUB_IP
     printf '%-8s %-6s %-18s %-14s %s\n' "$n" "$ROLE" "$MAC" "${IP:-dhcp}" \
      "$(running "$n" && echo running || echo stopped)")
  done
  echo
  if hub_reachable; then hub hubctl nodes || true; else echo "(hub $HUB_IP not reachable over SSH)"; hub_diag; fi
}

# hub registry first; a VM of this PC that has not joined yet is still reachable at its own IP
node_ip() {
  local ip
  if [ "$1" = hub ]; then echo "$HUB_IP"; return; fi
  ip=$(hub hubctl node ip "$1" 2>/dev/null) && [ -n "$ip" ] && { echo "$ip"; return; }
  [ -f "$VMS/$1.env" ] && ip=$(sed -n 's/^IP=//p' "$VMS/$1.env") && [ -n "$ip" ] && { echo "$ip"; return; }
  return 1
}

cmd_logs() {
  case ${1:-} in
    "")
      [ -d "$LOGS" ] || { echo "no logs yet"; return; }
      echo "logs/ on host $HOST_ID:"
      find "$LOGS" -type f -printf '  %TY-%Tm-%Td %TH:%TM  %8s  %P\n' | sort -k4 ;;
    tail)
      [ -n "${2:-}" ] || die "usage: $0 logs tail NAME"
      [ -f "$LOGS/vm/$2.console.log" ] || die "no console log for $2 on this PC"
      exec tail -n 40 -F "$LOGS/vm/$2.console.log" ;;
    collect)
      local who=${2:-all} names n ip d
      if [ "$who" = all ]; then
        names="hub $(hub hubctl nodes 2>/dev/null | awk 'NR > 2 {print $1}')"
      else names=$who; fi
      for n in $names; do
        ip=$(node_ip "$n" 2>/dev/null) || { echo "  $n: unknown node"; continue; }
        d=$LOGS/nodes/$n; mkdir -p "$d"
        # shellcheck disable=SC2016
        if lab_ssh "$ip" 'cd /var/log && tar czf - $(ls -d lab messages libvirt 2>/dev/null)' | tar xzf - -C "$d"; then
          echo "  $n: $(find "$d" -type f | wc -l) file(s) -> logs/nodes/$n/"
        else
          echo "  $n: unreachable ($ip)"
        fi
      done ;;
    commands) show_commands "${2:-}" ;;
    *) die "usage: $0 logs [tail NAME | collect [NAME|all] | commands [TOOL]]" ;;
  esac
}

# command journals on this PC: logs/commands (this PC), logs/build/<role>-commands
# (image builds), logs/nodes/<name>/lab/commands (collected from VMs)
journal_dirs() {
  local d
  for d in "$LOGS/commands" "$LOGS"/build/*-commands "$LOGS"/nodes/*/lab/commands; do
    [ -d "$d" ] && echo "$d"
  done
}
journal_src() {   # DIR -> short label
  case $1 in
    "$LOGS/commands") echo "pc$HOST_ID" ;;
    "$LOGS"/build/*) local b=${1##*/}; echo "build:${b%-commands}" ;;
    *) local n=${1#"$LOGS"/nodes/}; echo "${n%%/*}" ;;
  esac
}

show_commands() {   # [TOOL]
  local d f t
  mapfile -t dirs < <(journal_dirs)
  [ ${#dirs[@]} -gt 0 ] || { echo "no command journal yet (run something; VMs: logs collect)"; return; }
  if [ -z "$1" ]; then
    echo "command journal: calls per tool and source (logs collect pulls the VMs' journals)"
    local srcs=() d2
    for d in "${dirs[@]}"; do srcs+=("$(journal_src "$d")"); done
    printf '  %-20s' TOOL; for t in "${srcs[@]}"; do printf ' %9s' "${t:0:9}"; done; echo
    for t in $(for d in "${dirs[@]}"; do ls "$d"; done | grep -v '^all.log$' | sed 's/\.log$//' | sort -u); do
      printf '  %-20s' "$t"
      for d2 in "${dirs[@]}"; do
        f=$d2/$t.log
        if [ "$t" = files ]; then printf ' %9s' "$( [ -f "$f" ] && grep -c '> wrote ' "$f" || echo -)"
        else printf ' %9s' "$( [ -f "$f" ] && grep -c ' \$ ' "$f" || echo -)"; fi
      done
      echo
    done
    echo "  details: ./cluster.sh logs commands TOOL   (files = config files written; all.log per source = full order)"
  else
    t=$1
    for d in "${dirs[@]}"; do
      f=$d/$t.log; [ -f "$f" ] || continue
      sed "s|^|$(journal_src "$d")\t|" "$f"
    done | sort -s -t$'\t' -k2.1,2.19 | cut -f2- | grep . || echo "no '$t' calls journaled"
  fi
}

cmd_image() {   # add NAME FILE|URL [opts] | list
  case ${1:-list} in
    list) hub hubctl images ;;
    add)
      local name=${2:-} src=${3:-} size free up=/var/lib/lab/www/images/.${2:-x}.upload
      [ -n "$name" ] && [ -n "$src" ] || die "usage: $0 image add NAME FILE|URL [--type T] [--mem MB] [--disk SIZE] [--user LOGIN]"
      shift 3
      hub_reachable || die "hub not reachable"
      case $src in
        http://*|https://*)
          echo "the hub downloads $src"
          hub hubctl image add "$name" "$src" "$@" ;;
        *)
          [ -f "$src" ] || die "no file $src"
          qemu-img info "$src" 2>/dev/null | sed -n 's/^\(file format\|virtual size\): /  \1: /p'
          size=$(( $(stat -c %s "$src") / 1048576 ))
          free=$(hub "df -m /var/lib/lab | awk 'NR==2 {print \$4}'")
          [ "$free" -gt $((size * 2 + 200)) ] ||
            die "hub has ${free} MB free, needs ~$((size * 2 + 200)) MB for a ${size} MB image (bigger DISK_SIZE, rebuild the hub)"
          echo "uploading $src (${size} MB) to the hub"
          if command -v pv >/dev/null; then pv "$src"; else dd if="$src" bs=4M status=progress; fi |
            hub "cat > $up" || die "upload failed"
          hub hubctl image add "$name" --file "$up" "$@" ;;
      esac ;;
    *) die "usage: $0 image add NAME FILE|URL [...] | $0 images" ;;
  esac
}

cmd_host() {
  case ${1:-show} in
    init)
      local id=${2:-} nic=${3:-} role ip
      [[ $id =~ ^[0-9]+$ ]] && [ "$id" -ge 1 ] && [ "$id" -le 254 ] || die "usage: $0 host init ID(1..254) [UPLINK_NIC]"
      if [ "$id" = 1 ]; then role=main; else role=worker; fi
      ip=$(host_ip "$id")
      if [ -n "$nic" ]; then ip link show "${nic%%.*}" >/dev/null 2>&1 || die "no interface ${nic%%.*}"; fi
      cat >host.conf <<EOF
# this PC in the vdc cluster (per PC, never copied to other PCs)
HOST_ID=$id
HOST_ROLE=$role
LABPC_IP=$ip
UPLINK_NIC=$nic
EOF
      echo "host.conf: host $id ($role), underlay $ip, uplink ${nic:-none}"
      echo "spokes here: $(HOST_ID=$id; spoke_name 1) $(HOST_ID=$id; spoke_ip 1), $(HOST_ID=$id; spoke_name 2) $(HOST_ID=$id; spoke_ip 2), ...  MACs $(HOST_ID=$id; spoke_mac 1) ..."
      if [ "$role" = worker ]; then
        [ -n "$nic" ] || echo "warning: a worker without UPLINK_NIC cannot reach the hub (host init $id <NIC>)"
        echo "next: sudo ./cluster.sh net-up && sudo ./cluster.sh mtu-check && sudo ./cluster.sh spoke add"
      fi ;;
    show)
      echo "host $HOST_ID ($HOST_ROLE)  underlay $LABPC_IP  uplink ${UPLINK_NIC:-none}"
      echo "spokes      $(spoke_name 1) .. $(spoke_name 99)   MACs $(spoke_mac 1) ..   hub $HUB_IP"
      if [ "$IP_MODE" = static ]; then echo "addresses   static: $(spoke_ip 1) .. $(spoke_ip 99)/$UNDERLAY_PREFIX"
      else echo "addresses   DHCP from the hub ($DHCP_START - $DHCP_END)"; fi
      [ -f host.conf ] || echo "(no host.conf: defaults of a single-PC main host)" ;;
    export)
      [ "$HOST_ROLE" = main ] || die "export from the main PC"
      local f=${2:-vdc-host-bundle.tar}
      [ -f images/spoke.qcow2 ] || die "images/spoke.qcow2 missing (./cluster.sh build spoke)"
      [ -s "$ST/token" ] || die "run ./cluster.sh init first"
      tar -cf "$f" --owner=0 --group=0 cluster.conf cluster.sh README.md common hub spoke image \
        images/spoke.qcow2 state/token state/id_lab state/id_lab.pub
      chmod 600 "$f"
      echo "bundle $f ($(du -h "$f" | cut -f1)) - contains the join token and lab SSH key, keep it private"
      echo "on the other PC:  mkdir vdc-cluster && tar xf $(basename "$f") -C vdc-cluster && cd vdc-cluster"
      echo "                  sudo ./cluster.sh host init <ID> <UPLINK_NIC>" ;;
    *) die "usage: $0 host init ID [NIC] | show | export [FILE]" ;;
  esac
}

# ---------------------------------------------------------------- edge (main PC)
# The main PC is the edge between the campus network and the lab: its campus address is the
# entry point, and these ports on it are forwarded into the lab (from EDGE_ALLOW only):
#   EDGE_HUB_SSH (45022)          -> hub:22           staff SSH to the hub
#   JUMP_PORT (45222)             -> hub:45222        tenant jump host
#   WEBCONSOLE_PORTS (45300-45399)-> hub, same port   ttyd web consoles
#   one of VM_SSH_PORTS per guest -> <guest>:22       given by the hub (hubctl ports)
# It also routes the tenant subnets via the hub, so staff on this PC reach every guest.
# The rules live in two chains of our own, VDC-EDGE (nat) and VDC-EDGE-FWD (filter), rebuilt
# by 'edge sync' after every guest change.
EDGE_RT_PROTO=222          # our routes carry "proto 222" (ip route show proto 222)
: "${JUMP_PORT:=45222}" "${WEBCONSOLE_PORTS:=45300-45399}" "${EDGE_ALLOW:=0.0.0.0/0}"
EDGE_HUB_SSH=${EDGE_HUB_SSH-45022}
VM_SSH_PORTS=${VM_SSH_PORTS-46000-46999}
edge_if() {
  if [ -n "${EDGE_IF:-}" ]; then echo "$EDGE_IF"; return; fi
  ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
}
edge_addr() {
  local i; i=$(edge_if)
  [ -n "$i" ] && ip -4 -o addr show dev "$i" 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}'
}
edge_ipt() {   # TABLE CHAIN RULE...  (added once, on top)
  local t=$1 c=$2; shift 2
  iptables -t "$t" -C "$c" "$@" 2>/dev/null || iptables -t "$t" -I "$c" 1 "$@"
}
edge_clear() {
  local t r
  command -v iptables >/dev/null || return 0
  for t in nat filter; do
    iptables -t "$t" -S 2>/dev/null | grep -- '--comment vdc-edge' | sed 's/^-A /-D /' |
      while read -r r; do eval "iptables -t $t $r" 2>/dev/null || true; done
  done
  iptables -t nat -F VDC-EDGE 2>/dev/null && iptables -t nat -X VDC-EDGE 2>/dev/null || true
  iptables -t filter -F VDC-EDGE-FWD 2>/dev/null && iptables -t filter -X VDC-EDGE-FWD 2>/dev/null || true
}
edge_forward_restore() {
  if [ -f "$ST/edge.ip_forward" ]; then
    sysctl -qw "net.ipv4.ip_forward=$(cat "$ST/edge.ip_forward")" 2>/dev/null || true
    rm -f "$ST/edge.ip_forward"
  fi
}
edge_on() { iptables -t nat -S PREROUTING 2>/dev/null | grep -q -- '-j VDC-EDGE'; }
edge_routes() {   # tenant subnets via the hub, from the hub's registry
  local sn tenants err
  [ "$HOST_ROLE" = main ] && ip link show "$UNDERLAY_BR" >/dev/null 2>&1 || return 0
  ip -4 -o addr show dev "$UNDERLAY_BR" | grep -q " $LABPC_IP/" ||
    { echo "warning: $UNDERLAY_BR does not have $LABPC_IP: ./cluster.sh net-up" >&2; return 0; }
  underlay_route 2>/dev/null || true
  if ! tenants=$(hub cat /var/lib/lab/tenants 2>&1); then
    echo "warning: no tenant routes, the hub did not answer: $tenants" >&2; return 0
  fi
  ip route flush proto "$EDGE_RT_PROTO" 2>/dev/null || true
  for sn in $(echo "$tenants" | awk 'NF >= 2 {print ($3 != "" ? $3 : "192.168." $2 ".0/24")}'); do
    # src: the hub lets only LABPC_IP into tenant subnets, whatever else br-under carries
    err=$(ip route replace "$sn" via "$HUB_IP" dev "$UNDERLAY_BR" src "$LABPC_IP" proto "$EDGE_RT_PROTO" 2>&1) ||
      echo "warning: route $sn via $HUB_IP: $err" >&2
  done
}
edge_guest_ports() {   # "PORT ADDRESS GUEST" per guest, from the hub
  [ -n "$VM_SSH_PORTS" ] || return 0
  hub hubctl ports 2>/dev/null | awk 'NR > 1 && $4 ~ /^[0-9]+$/ && $3 ~ /^[0-9.]+$/ {print $4, $3, $1}'
}
edge_rules() {   # CAMPUS_IF: (re)write the two chains; the jumps into them are added by 'edge up'
  local i=$1 n=0 port addr guest
  iptables -t nat -N VDC-EDGE 2>/dev/null || iptables -t nat -F VDC-EDGE
  iptables -t filter -N VDC-EDGE-FWD 2>/dev/null || iptables -t filter -F VDC-EDGE-FWD
  # what was forwarded may pass, and its answers may come back (ctstate DNAT: both directions)
  iptables -t filter -A VDC-EDGE-FWD -i "$i" -o "$UNDERLAY_BR" -m conntrack --ctstate DNAT -j ACCEPT
  iptables -t filter -A VDC-EDGE-FWD -i "$UNDERLAY_BR" -o "$i" -m conntrack --ctstate DNAT -j ACCEPT
  if [ -n "$EDGE_HUB_SSH" ]; then
    iptables -t nat -A VDC-EDGE -p tcp --dport "$EDGE_HUB_SSH" -j DNAT --to-destination "$HUB_IP:22"
  fi
  iptables -t nat -A VDC-EDGE -p tcp --dport "$JUMP_PORT" -j DNAT --to-destination "$HUB_IP:$JUMP_PORT"
  if [ -n "$WEBCONSOLE_PORTS" ]; then   # no port in --to-destination: each keeps its own
    iptables -t nat -A VDC-EDGE -p tcp --dport "${WEBCONSOLE_PORTS/-/:}" -j DNAT --to-destination "$HUB_IP"
  fi
  while read -r port addr guest; do
    [ -n "$port" ] || continue
    iptables -t nat -A VDC-EDGE -p tcp --dport "$port" -m comment --comment "guest $guest" \
      -j DNAT --to-destination "$addr:22"
    n=$((n + 1))
  done < <(edge_guest_ports)
  echo "$n"
}

cmd_edge() {
  [ "$HOST_ROLE" = main ] || die "the edge is the main PC (it runs the hub)"
  local i a n
  case ${1:-status} in
    up|sync|routes)
      edge_routes
      i=$(edge_if || true)
      if [ -z "$i" ]; then
        echo "edge: no campus NIC (EDGE_IF is empty and there is no default route)"; return 0
      fi
      command -v iptables >/dev/null || die "the edge needs iptables"
      if [ "$1" != up ] && ! edge_on; then
        echo "edge: routes updated; the forwards are off (./cluster.sh edge up)"; return 0
      fi
      if [ "$1" = up ]; then
        step "edge: campus $i -> hub and guests, from $EDGE_ALLOW"
        edge_clear          # the campus address or EDGE_ALLOW may have changed
      fi
      # the one kernel setting the edge changes: remembered, and restored by 'edge down'
      [ -f "$ST/edge.ip_forward" ] || cat /proc/sys/net/ipv4/ip_forward >"$ST/edge.ip_forward"
      sysctl -qw net.ipv4.ip_forward=1
      n=$(edge_rules "$i")
      # only connections from EDGE_ALLOW to this PC's own address enter VDC-EDGE
      a=$(edge_addr || true)
      edge_ipt nat PREROUTING -i "$i" ${a:+-d "$a"} -s "$EDGE_ALLOW" -m comment --comment vdc-edge -j VDC-EDGE
      # from the lab's view the connection comes from this PC (LABPC_IP), so replies return here
      edge_ipt nat POSTROUTING -o "$UNDERLAY_BR" -m conntrack --ctstate DNAT -m comment --comment vdc-edge -j SNAT --to-source "$LABPC_IP"
      edge_ipt filter FORWARD -m comment --comment vdc-edge -j VDC-EDGE-FWD
      if [ "$1" = up ]; then
        echo "edge up: ${a:-<this PC>} forwards hub SSH :${EDGE_HUB_SSH:--}, jump host :$JUMP_PORT," \
             "consoles :$WEBCONSOLE_PORTS, $n guest SSH port(s); from $EDGE_ALLOW"
      fi ;;
    down)
      edge_clear
      ip route flush proto "$EDGE_RT_PROTO" 2>/dev/null || true
      edge_forward_restore
      echo "edge down: forwards and tenant routes removed" ;;
    status)
      i=$(edge_if || true); a=$(edge_addr || true)
      echo "campus NIC  ${i:-none}${a:+ ($a)}   allowed from $EDGE_ALLOW"
      if edge_on; then
        [ -z "$EDGE_HUB_SSH" ] || echo "hub ssh     ssh -p $EDGE_HUB_SSH root@${a:-<this PC>}"
        echo "jump host   ssh -J t-<tenant>@${a:-<this PC>}:$JUMP_PORT <user>@<guest>"
        echo "consoles    ${a:-<this PC>}:$WEBCONSOLE_PORTS -> $HUB_IP (hubctl consoles)"
        iptables -t nat -S VDC-EDGE 2>/dev/null | awk -v a="${a:-<this PC>}" '
          /--comment "?guest / {for (k = 1; k <= NF; k++) {if ($k == "--dport") p = $(k + 1); if ($k ~ /^"?guest$/) g = $(k + 1); if ($k == "--to-destination") d = $(k + 1)}
            gsub(/"/, "", g); printf "guest ssh   %-14s ssh -p %s <user>@%s   -> %s\n", g, p, a, d}'
      else
        echo "forwards    off (./cluster.sh edge up)"
      fi
      echo "routes      $(ip route show proto "$EDGE_RT_PROTO" 2>/dev/null | awk '{print $1}' | tr '\n' ' ')" ;;
    *) die "usage: $0 edge up|down|status|sync" ;;
  esac
}

# ---------------------------------------------------------------- update running VMs
# Copies the lab scripts and cluster.conf into the running hub and spokes and restarts
# their services, so changes reach a cluster without rebuilding it (and losing the hub's
# tenants). New packages are added where needed (ttyd on the hub).
UPDATE_HUB='set -e; K=/tmp/kit-update
cp -a $K/common/rootfs/. /; cp -a $K/hub/rootfs/. /
chmod 755 /usr/local/sbin/* /etc/init.d/lab-* /usr/local/lib/lab/cgi.sh /var/lib/lab/www/cgi-bin/*
install -m 600 $K/cluster.conf /etc/lab/cluster.env
/usr/local/lib/lab/lab-trace --install /usr/local/lib/lab/trace.d >/dev/null 2>&1 || true
export LAB_TRACE_CTX="cluster.sh update"
/usr/local/sbin/lab-hub-config --running
rc-service lab-api restart >/dev/null
rm -rf $K'
UPDATE_SPOKE='set -e; K=/tmp/kit-update
cp -a $K/common/rootfs/. /; cp -a $K/spoke/rootfs/. /
chmod 755 /usr/local/sbin/* /etc/init.d/lab-*
install -m 600 $K/cluster.conf /etc/lab/cluster.env
/usr/local/lib/lab/lab-trace --install /usr/local/lib/lab/trace.d >/dev/null 2>&1 || true
rc-service lab-agent restart >/dev/null
rm -rf $K'

update_node() {   # NAME IP SCRIPT TARBALL
  if lab_ssh "$2" "rm -rf /tmp/kit-update && mkdir -p /tmp/kit-update && tar -xzf - -C /tmp/kit-update && sh -c '$3'" <"$4" \
      >>"$LOGS/update.log" 2>&1; then
    echo "  $1  updated"
  else
    echo "  $1  FAILED (logs/update.log)"; return 1
  fi
}

cmd_update() {
  [ "$HOST_ROLE" = main ] || die "update runs on the main PC (it reaches the hub and every spoke)"
  local what=${1:-all} tarf n ip fails=0
  case $what in all|hub|spokes) ;; *) die "usage: $0 update [all|hub|spokes]" ;; esac
  hub_reachable || die "the hub does not answer SSH ($0 list)"
  tarf=$(mktemp)
  tar -C "$KIT" -czf "$tarf" cluster.conf common/rootfs hub/rootfs spoke/rootfs
  echo "== update: lab scripts and cluster.conf into the running VMs (log: logs/update.log)"
  if [ "$what" != spokes ]; then
    step "update hub"
    update_node hub "$HUB_IP" "$UPDATE_HUB" "$tarf" || fails=1
  fi
  if [ "$what" != hub ]; then
    while read -r n ip _; do
      [ -n "$n" ] || continue
      step "update spoke $n"
      update_node "$n" "$ip" "$UPDATE_SPOKE" "$tarf" || fails=1
    done < <(hub cat /var/lib/lab/nodes)
  fi
  rm -f "$tarf"
  if [ "${EDGE:-yes}" = yes ]; then cmd_edge up; fi
  if [ -x "$CVENV/bin/python" ] && cloud_pid api >/dev/null; then echo "== cloud API"; cmd_cloud restart; fi
  [ "$fails" = 0 ] || die "some nodes were not updated"
  echo "done. Existing guests keep their old network settings until recreated (see README: Access to guests)"
}

# ---------------------------------------------------------------- cloud API (cloud/)
# The FastAPI service in cloud/ (NIST CCRA: /tenant /provider /audit /carrier) runs on the main
# PC next to the kit. Code in cloud/; everything it creates in state/cloud/ and logs/cloud/:
# its own Python environment and pip cache, database, secrets, a private Redis (job queue, on a
# Unix socket, no TCP port) and cloudflared (tunnels). It installs no system packages and uses
# no system services.
CLOUD=$KIT/cloud
CST=$ST/cloud
CVENV=$CST/venv
: "${CLOUD_PORT:=36100}" "${CLOUD_BIND:=0.0.0.0}" "${CLOUD_ADMIN_USER:=admin}"
: "${CLOUD_VNI_MIN:=100}" "${CLOUD_VNI_MAX:=254}"
: "${CLOUD_SUBNET_TEMPLATE:=10.{vni}.20.0/24}" "${CLOUD_METER_MINUTES:=5}"
: "${CLOUD_QUEUE:=redis}" "${CLOUD_TUNNELS:=yes}" "${CLOUD_TUNNEL_HOURS:=8}" "${CLOUD_TUNNELS_PER_TENANT:=2}"
: "${CLOUD_TUNNEL_PROTOCOL:=auto}"
if [ "$CLOUD_QUEUE" = redis ]; then CLOUD_PROCS="redis api worker meter"; else CLOUD_PROCS="api worker meter"; fi
CLOUD_REDIS_SOCK=$CST/redis.sock
CLOUD_REDIS_URL="unix://$CLOUD_REDIS_SOCK?db=0"
# Python stays inside the folder: no user site-packages, pip cache and bytecode in state/cloud/
export PYTHONNOUSERSITE=1 PIP_CACHE_DIR=$CST/pip-cache PYTHONPYCACHEPREFIX=$CST/pycache PIP_DISABLE_PIP_VERSION_CHECK=1

cloud_env_file() {   # state/cloud/cloud.env: the processes' settings (also the systemd EnvironmentFile)
  ( umask 077
    cat >"$CST/cloud.env" <<ENV
VDC_KIT=$KIT
VDC_HUB=$HUB_IP
VDC_DB=sqlite:///$CST/cloud.db
PYTHONNOUSERSITE=1
PYTHONPYCACHEPREFIX=$CST/pycache
VDC_JWT_SECRET=$(cat "$CST/jwt-secret")
VDC_ADMIN_USER=$CLOUD_ADMIN_USER
VDC_ADMIN_PASSWORD=$(cat "$CST/admin-password")
VDC_VNI_MIN=$CLOUD_VNI_MIN
VDC_VNI_MAX=$CLOUD_VNI_MAX
VDC_SUBNET_TEMPLATE=$CLOUD_SUBNET_TEMPLATE
VDC_METER_MINUTES=$CLOUD_METER_MINUTES
VDC_SUDO=
VDC_EDGE_HOST=$(edge_addr || true)
VDC_JUMP_PORT=$JUMP_PORT
VDC_EDGE=${EDGE:-yes}
VDC_WEBCONSOLE_PORTS=$WEBCONSOLE_PORTS
VDC_WEBCONSOLE_MINUTES=${WEBCONSOLE_MINUTES:-60}
VDC_GUEST_PASSWORD=${GUEST_PASSWORD:-vdclab}
VDC_QUEUE_BACKEND=$CLOUD_QUEUE
REDIS_URL=$CLOUD_REDIS_URL
VDC_API_PORT=$CLOUD_PORT
VDC_TUNNELS=$CLOUD_TUNNELS
VDC_CLOUDFLARED=$(cloudflared_bin || true)
VDC_TUNNEL_DIR=$CST/tunnels
VDC_TUNNEL_HOURS=$CLOUD_TUNNEL_HOURS
VDC_TUNNELS_PER_TENANT=$CLOUD_TUNNELS_PER_TENANT
VDC_TUNNEL_PROTOCOL=$CLOUD_TUNNEL_PROTOCOL
ENV
  )
}

# ---- the private Redis: a binary on PATH or one built into state/cloud/redis; its own config,
#      data and socket in state/cloud/ (not the system's Redis service)
redis_bin() {
  if [ -x "$CST/redis/bin/redis-server" ]; then echo "$CST/redis/bin/redis-server"; return; fi
  command -v redis-server
}
redis_conf() {
  cat >"$CST/redis.conf" <<RCONF
# private Redis of the vdc-cluster cloud API (job queue) - written by cluster.sh
port 0
unixsocket $CLOUD_REDIS_SOCK
unixsocketperm 700
dir $CST
dbfilename redis.rdb
save 60 1
appendonly no
daemonize no
logfile ""
protected-mode yes
RCONF
}
redis_build() {   # from source, into state/cloud/redis (needs make and a C compiler)
  local src=$CST/redis-src
  command -v make >/dev/null && command -v cc >/dev/null ||
    die "no redis-server on this PC and no make/cc to build one: put a redis-server binary on PATH, or set CLOUD_QUEUE=sqlite"
  echo "  building Redis from source into state/cloud/redis (once, about a minute)"
  rm -rf "$src"; mkdir -p "$src" "$CST/redis/bin"
  "$CVENV/bin/python" -c "import urllib.request, sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
      https://download.redis.io/redis-stable.tar.gz "$src/redis.tgz" ||
    "$CVENV/bin/python" -c "import urllib.request, sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
      https://github.com/redis/redis/archive/refs/tags/7.2.6.tar.gz "$src/redis.tgz" ||
    die "cannot download Redis (no internet?)"
  tar -xzf "$src/redis.tgz" -C "$src" --strip-components=1
  make -C "$src/src" -j"$(nproc)" BUILD_TLS=no MALLOC=libc redis-server >"$LOGS/cloud/redis-build.log" 2>&1 ||
    die "the Redis build failed (logs/cloud/redis-build.log)"
  cp "$src/src/redis-server" "$CST/redis/bin/"
  rm -rf "$src"
}
redis_up() { "$CVENV/bin/python" -c "import redis, sys; redis.from_url(sys.argv[1]).ping()" "$CLOUD_REDIS_URL" 2>/dev/null; }

# ---- cloudflared, for Cloudflare quick tunnels (https://<random>.trycloudflare.com, no account)
cloudflared_bin() {
  if [ -x "$CST/bin/cloudflared" ]; then echo "$CST/bin/cloudflared"; return; fi
  command -v cloudflared
}
cloudflared_get() {
  local arch b=$CST/bin/cloudflared
  [ "$CLOUD_TUNNELS" = yes ] || return 0
  [ -x "$b" ] && return 0
  case $(uname -m) in
    x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; armv7l) arch=arm ;;
    *) echo "  note: no cloudflared build for $(uname -m): tunnels will not work"; return 0 ;;
  esac
  echo "  cloudflared into state/cloud/bin (Cloudflare quick tunnels)"
  mkdir -p "$CST/bin"
  if "$CVENV/bin/python" -c "import urllib.request, sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
       "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch" "$b.part" &&
     chmod 755 "$b.part" && "$b.part" --version >/dev/null 2>&1; then
    mv "$b.part" "$b"
  else
    rm -f "$b.part"
    echo "  warning: cloudflared download failed: tunnels will not work until 'cloud install' succeeds"
  fi
}
tunnels_stop() {   # the tunnel processes the API started (it marks them closed on its next start)
  local f
  for f in "$CST"/tunnels/*.pid; do
    [ -f "$f" ] || continue
    kill -TERM -- "-$(cat "$f")" 2>/dev/null || kill "$(cat "$f")" 2>/dev/null || true
    rm -f "$f"
  done
}

cloud_cmd() {   # PROC -> the command line of one process
  case $1 in
    redis)  echo "$(redis_bin) $CST/redis.conf" ;;
    api)    echo "$CVENV/bin/uvicorn app.main:app --host $CLOUD_BIND --port $CLOUD_PORT" ;;
    worker) echo "$CVENV/bin/python -m app.worker" ;;
    meter)  echo "$CVENV/bin/python -m app.metering" ;;
  esac
}

cloud_pid() { local p=$CST/$1.pid; [ -f "$p" ] && kill -0 "$(cat "$p")" 2>/dev/null && cat "$p"; }
cloud_systemd() { systemctl is-active --quiet "vdc-cloud-$1" 2>/dev/null; }
cloud_venv() {   # a private Python environment in state/cloud/venv (no system packages)
  [ -x "$CVENV/bin/python" ] && [ -x "$CVENV/bin/pip" ] && return 0
  command -v python3 >/dev/null || die "python3 is needed (3.10 or newer)"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "python3 is too old: 3.10 or newer is needed"
  rm -rf "$CVENV"
  if python3 -m venv "$CVENV" 2>/dev/null; then return 0; fi
  # Debian/Ubuntu without python3-venv: a venv without pip, then pip from PyPI into it
  echo "  (python3 has no ensurepip: bootstrapping pip inside state/cloud/venv)"
  rm -rf "$CVENV"
  python3 -m venv --without-pip "$CVENV" || die "python3 -m venv does not work here"
  "$CVENV/bin/python" -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', '$CST/get-pip.py')" ||
    die "cannot download get-pip.py (no internet?)"
  "$CVENV/bin/python" "$CST/get-pip.py" -q || die "pip bootstrap failed"
  rm -f "$CST/get-pip.py"
}

cloud_unit() {   # PROC -> systemd unit text
  cat <<UNIT
[Unit]
Description=vdc-cloud $1 ($KIT)
After=network-online.target$([ "$1" = redis ] || [ "$CLOUD_QUEUE" != redis ] || echo " vdc-cloud-redis.service")

[Service]
EnvironmentFile=$CST/cloud.env
WorkingDirectory=$CLOUD
ExecStart=$(cloud_cmd "$1")
StandardOutput=append:$LOGS/cloud/$1.log
StandardError=append:$LOGS/cloud/$1.log
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
}

cmd_cloud() {
  [ "$HOST_ROLE" = main ] || die "the cloud API runs on the main PC (it drives the hub)"
  mkdir -p "$CST" "$LOGS/cloud"; chmod 700 "$CST"
  local p pid i
  case ${1:-status} in
    install)
      echo "== cloud install: Python environment, Redis, cloudflared, database and secrets, all in state/cloud/"
      cloud_venv
      echo "  pip install -r cloud/requirements.txt"
      "$CVENV/bin/pip" install -q --upgrade pip
      "$CVENV/bin/pip" install -q -r "$CLOUD/requirements.txt"
      if [ "${2:-}" = --dev ]; then "$CVENV/bin/pip" install -q -r "$CLOUD/requirements-dev.txt"; fi
      if [ "$CLOUD_QUEUE" = redis ]; then
        if redis_bin >/dev/null; then echo "  redis-server: $(redis_bin)"; else redis_build; fi
      fi
      cloudflared_get
      [ -s "$CST/jwt-secret" ] || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$CST/jwt-secret"
      [ -s "$CST/admin-password" ] || head -c 12 /dev/urandom | base64 | tr -d '/+=' | cut -c1-14 >"$CST/admin-password"
      chmod 600 "$CST/jwt-secret" "$CST/admin-password"
      cloud_env_file
      echo "installed. provider login: $CLOUD_ADMIN_USER / $(cat "$CST/admin-password")  (state/cloud/admin-password)"
      echo "next: ./cluster.sh cloud start" ;;
    start)
      [ -x "$CVENV/bin/uvicorn" ] || die "not installed: ./cluster.sh cloud install"
      cloud_env_file
      echo "== cloud start"
      if [ "$CLOUD_QUEUE" = redis ]; then
        redis_bin >/dev/null || die "no redis-server: ./cluster.sh cloud install (or CLOUD_QUEUE=sqlite)"
        redis_conf
      fi
      for p in $CLOUD_PROCS; do
        if cloud_systemd "$p"; then echo "  $p  runs under systemd (vdc-cloud-$p)"; continue; fi
        if pid=$(cloud_pid "$p"); then echo "  $p  already running (pid $pid)"; continue; fi
        # shellcheck disable=SC2046
        (cd "$CLOUD" && set -a && . "$CST/cloud.env" && set +a &&
          exec setsid $(cloud_cmd "$p") >>"$LOGS/cloud/$p.log" 2>&1 </dev/null) &
        echo $! >"$CST/$p.pid"
        echo "  $p  started (pid $!, log logs/cloud/$p.log)"
        if [ "$p" = redis ]; then
          for i in $(seq 20); do redis_up && break; sleep 0.5; done
          redis_up || die "the private Redis did not start (logs/cloud/redis.log)"
        fi
      done
      for i in $(seq 20); do
        "$CVENV/bin/python" -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=2)" \
          "http://127.0.0.1:$CLOUD_PORT/" 2>/dev/null && break
        sleep 1
      done
      [ "$i" -lt 20 ] || die "the API does not answer on port $CLOUD_PORT: tail logs/cloud/api.log"
      echo "cloud API up: http://$LABPC_IP:$CLOUD_PORT/docs   (provider: $CLOUD_ADMIN_USER / $(cat "$CST/admin-password"))" ;;
    stop)
      tunnels_stop
      for p in $(echo "$CLOUD_PROCS" | tr ' ' '\n' | tac); do    # Redis last
        if pid=$(cloud_pid "$p"); then kill "$pid" 2>/dev/null || true; echo "  $p  stopped"; fi
        rm -f "$CST/$p.pid"
      done ;;
    restart) cmd_cloud stop; cmd_cloud start ;;
    status)
      [ -x "$CVENV/bin/python" ] || { echo "cloud API: not installed (./cluster.sh cloud install)"; return 0; }
      for p in $CLOUD_PROCS; do
        if cloud_systemd "$p"; then echo "  $p  running (systemd vdc-cloud-$p)"
        elif pid=$(cloud_pid "$p"); then echo "  $p  running (pid $pid)"
        else echo "  $p  stopped"; fi
      done
      if [ "$CLOUD_QUEUE" = redis ]; then
        if redis_up; then echo "  queue   Redis on state/cloud/redis.sock"; else echo "  queue   Redis not reachable"; fi
      else echo "  queue   SQLite (CLOUD_QUEUE=sqlite)"; fi
      echo "  tunnels $(ls "$CST"/tunnels/*.pid 2>/dev/null | wc -l) running, cloudflared: $(cloudflared_bin || echo missing)"
      echo "  url     http://$LABPC_IP:$CLOUD_PORT/docs" ;;
    logs)
      p=${2:-api}
      case " $CLOUD_PROCS " in *" $p "*) ;; *) die "usage: $0 cloud logs [$(echo "$CLOUD_PROCS" | tr ' ' '|')]" ;; esac
      exec tail -n 40 -F "$LOGS/cloud/$p.log" ;;
    admin) echo "provider login: $CLOUD_ADMIN_USER / $(cat "$CST/admin-password" 2>/dev/null || echo '(not installed)')" ;;
    enable)   # the processes under systemd: they survive reboots (the one step outside
              # the folder: unit files in /etc/systemd/system, removed again by 'cloud disable')
      [ -x "$CVENV/bin/uvicorn" ] || die "not installed: ./cluster.sh cloud install"
      command -v systemctl >/dev/null || die "no systemd on this PC: use ./cluster.sh cloud start"
      cloud_env_file
      cmd_cloud stop >/dev/null
      for p in $CLOUD_PROCS; do cloud_unit "$p" >"/etc/systemd/system/vdc-cloud-$p.service"; done
      systemctl daemon-reload
      for p in $CLOUD_PROCS; do systemctl enable --now "vdc-cloud-$p" >/dev/null 2>&1; done
      echo "systemd: $(for p in $CLOUD_PROCS; do printf 'vdc-cloud-%s ' "$p"; done)enabled and started"
      echo "  note: the hub VM is not started at boot; after a reboot run ./cluster.sh up" ;;
    disable)
      for p in $CLOUD_PROCS; do
        systemctl disable --now "vdc-cloud-$p" 2>/dev/null || true
        rm -f "/etc/systemd/system/vdc-cloud-$p.service"
      done
      systemctl daemon-reload 2>/dev/null || true
      echo "systemd units removed (start by hand: ./cluster.sh cloud start)" ;;
    test)
      [ -x "$CVENV/bin/python" ] || die "not installed: ./cluster.sh cloud install --dev"
      [ -x "$CVENV/bin/pytest" ] || "$CVENV/bin/pip" install -q -r "$CLOUD/requirements-dev.txt"
      (cd "$CLOUD" && "$CVENV/bin/pytest" -q -p no:cacheprovider tests) ;;
    *) die "usage: $0 cloud install [--dev]|start|stop|restart|status|logs [PROC]|admin|enable|disable|test" ;;
  esac
}

ARGS=("$@")
check_host
case ${1:-help} in
  console|ssh|hubctl|capture|help|-h|--help) ;;
  logs) if [ "${2:-}" != tail ] && [ "${2:-}" != commands ]; then need_root; start_log "$*"; fi ;;
  cloud) need_root; case ${2:-status} in logs|status|admin) ;; *) start_log "$*" ;; esac ;;
  *) need_root; start_log "$*"; trace_setup "$@" ;;
esac
case ${1:-help} in
  init) need_root; cmd_init ;;
  build) shift; image/build.sh "${1:-all}" ;;
  net-up) cmd_net_up ;;
  net-down) cmd_net_down ;;
  mtu-check) cmd_mtu_check "${2:-}" ;;
  cloud) shift; cmd_cloud "$@" ;;
  edge) shift; cmd_edge "$@" ;;
  update) shift; cmd_update "$@" ;;
  up) cmd_up ;;
  spoke) [ "${2:-}" = add ] || die "usage: $0 spoke add [COUNT]"; cmd_spoke_add "${3:-1}" ;;
  start) [ -n "${2:-}" ] || die "usage: $0 start NAME|all"; cmd_start "$2" ;;
  stop)
    [ -n "${2:-}" ] || die "usage: $0 stop NAME|all"
    if [ "$2" = all ]; then   # spokes first, hub last
      for n in $(vm_names | grep -vx hub || true) $(vm_names | grep -x hub || true); do vm_stop "$n"; done
    else vm_stop "$2"; fi ;;
  rm) [ -n "${2:-}" ] || die "usage: $0 rm NAME"; cmd_rm "$2" ;;
  list|ls) cmd_list ;;
  logs) shift; cmd_logs "$@" ;;
  host) shift; cmd_host "$@" ;;
  console) need_root; [ -n "${2:-}" ] || die "usage: $0 console NAME"
    echo "(Ctrl-] to leave)"; exec socat -,raw,echo=0,escape=0x1d "UNIX-CONNECT:$VMS/$2.serial" ;;
  ssh) need_root; [ -n "${2:-}" ] || die "usage: $0 ssh NAME [CMD]"
    ip=$(node_ip "$2") || die "unknown node $2"; shift 2; lab_ssh_t "$ip" "$@" ;;
  hubctl) need_root; shift; rc=0; lab_ssh_t "$HUB_IP" hubctl "$@" || rc=$?
    case "${1:-} ${2:-}" in "tenant add"|"tenant del"|"vm add"|"vm del") cmd_edge sync >/dev/null || true ;; esac
    if [ "${1:-} ${2:-}" = "vm add" ] && [ "$rc" = 0 ] && edge_on; then   # the real campus address
      cmd_edge status | awk -v g="${3:-}" '$1 == "guest" && $3 == g {sub(/^guest ssh +[^ ]+ +/, ""); print "forwarded:  " $0}'
    fi
    exit "$rc" ;;
  image) shift; cmd_image "$@" ;;
  images) cmd_image list ;;
  capture) need_root; [ -n "${2:-}" ] || die "usage: $0 capture NAME [FILTER]"
    n=$2; shift 2; exec tcpdump -ni "tap-$n" "$@" ;;
  help|-h|--help) awk 'NR > 1 && !/^#/ {exit} NR > 1 {sub(/^# ?/, ""); print}' "$0" ;;
  *) awk 'NR > 1 && !/^#/ {exit} NR > 1 {sub(/^# ?/, ""); print}' "$0"; exit 1 ;;
esac
