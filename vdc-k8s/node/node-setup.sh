#!/bin/sh
# node-setup.sh: makes a guest a Kubernetes node of vdc-k8s and installs k3s. Runs as root over
# SSH, after vdc-node is in /usr/local/bin. vdc-k8s sets these before it:
#   ROLE        server (control plane) or agent (worker)
#   NODE_IP     the node's address in the tenant subnet
#   K3S_URL, K3S_TOKEN     (agent) where and how to join
#   TLS_SANS    (server) more names for the API certificate, space-separated (a floating IP)
#   INGRESS     (server) yes: keep k3s' Traefik ingress controller
#   K3S_CHANNEL / K3S_VERSION   which k3s (default: the stable channel)
# Alpine (apk, OpenRC) or Ubuntu/Debian (apt, systemd). Without a baked image (image/) k3s is
# downloaded now, so the tenant needs internet for this step.
set -eu
die() { echo "node-setup: $*" >&2; exit 1; }
: "${ROLE:?}" "${NODE_IP:?}"

if [ -f /etc/alpine-release ]; then
  command -v curl >/dev/null || { echo "installing curl (apk)"; apk add -q curl; }
  # k3s needs the cgroup filesystem mounted at boot
  rc-update add cgroups boot >/dev/null 2>&1 || true
  rc-service cgroups start >/dev/null 2>&1 || true
elif command -v apt-get >/dev/null; then
  command -v curl >/dev/null || { apt-get update -q; apt-get install -y -q curl; }
else
  die "not Alpine, Ubuntu or Debian"
fi

printf 'br_netfilter\noverlay\n' >/etc/modules-load.d/k3s.conf
modprobe br_netfilter 2>/dev/null || true
modprobe overlay 2>/dev/null || true
cat >/etc/sysctl.d/90-k3s.conf <<'SYS'
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
SYS
sysctl -q -p /etc/sysctl.d/90-k3s.conf 2>/dev/null || true
# every node needs its own machine-id (an image's clones may share one, or have none)
[ -s /etc/machine-id ] || tr -d - </proc/sys/kernel/random/uuid >/etc/machine-id

installer=/usr/local/share/vdc/k3s-install.sh            # baked into the image, or fetched now
if [ -x /usr/local/bin/k3s ] && [ -s "$installer" ]; then
  export INSTALL_K3S_SKIP_DOWNLOAD=true
  echo "k3s is in the image: $(k3s --version | head -n 1)"
else
  echo "downloading k3s (${K3S_VERSION:-${K3S_CHANNEL:-stable} channel})"
  installer=/tmp/k3s-install.sh
  curl -sfL https://get.k3s.io -o "$installer" || die "can't reach get.k3s.io (internet off?)"
fi
[ -z "${K3S_VERSION:-}" ] || export INSTALL_K3S_VERSION="$K3S_VERSION"
export INSTALL_K3S_CHANNEL="${K3S_CHANNEL:-stable}"

if [ "$ROLE" = server ]; then
  # host-gw: all nodes share the tenant's L2 segment, so pod traffic needs no extra tunnel;
  # no ServiceLB: MetalLB hands out the tenant's extra addresses instead
  exec_args="server --node-ip $NODE_IP --flannel-backend=host-gw --disable=servicelb --write-kubeconfig-mode 600 --tls-san 127.0.0.1 --tls-san $NODE_IP"
  for s in ${TLS_SANS:-}; do exec_args="$exec_args --tls-san $s"; done
  [ "${INGRESS:-no}" = yes ] || exec_args="$exec_args --disable=traefik"
  INSTALL_K3S_EXEC="$exec_args" sh "$installer" >/tmp/k3s-install.log 2>&1 || { tail -n 5 /tmp/k3s-install.log >&2; die "the k3s installer failed"; }
  i=0
  until k3s kubectl get nodes >/dev/null 2>&1; do
    i=$((i + 1)); [ $i -lt 120 ] || die "the k3s server did not come up (/var/log/k3s.log)"
    sleep 2
  done
else
  : "${K3S_URL:?}" "${K3S_TOKEN:?}"
  K3S_URL=$K3S_URL K3S_TOKEN=$K3S_TOKEN INSTALL_K3S_EXEC="agent --node-ip $NODE_IP" sh "$installer" >/tmp/k3s-install.log 2>&1 ||
    { tail -n 5 /tmp/k3s-install.log >&2; die "the k3s installer failed"; }
fi
/usr/local/bin/vdc-node check
