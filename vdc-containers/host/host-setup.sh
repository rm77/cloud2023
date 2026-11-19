#!/bin/sh
# host-setup.sh: makes a guest a container host for vdc-containers. Runs as root over SSH,
# after vdc-host is in /usr/local/bin; safe to run again. Alpine (apk, OpenRC) or Ubuntu/Debian
# (apt, systemd). Without a baked image (image/), Docker comes from the distribution's
# repositories, so the tenant needs internet for this step.
set -eu
die() { echo "host-setup: $*" >&2; exit 1; }
have_docker() { command -v docker >/dev/null && docker compose version >/dev/null 2>&1; }

if [ -f /etc/alpine-release ]; then
  if ! have_docker; then
    echo "installing docker and docker compose (apk)"
    # docker is in the community repository
    sed -i -E 's|^#[[:space:]]*(http.*/community)[[:space:]]*$|\1|' /etc/apk/repositories
    apk update -q
    apk add -q docker docker-cli-compose
  fi
  rc-update add docker default >/dev/null 2>&1 || true
  rc-update add local default >/dev/null 2>&1 || true
  mkdir -p /etc/local.d
  printf '#!/bin/sh\n/usr/local/bin/vdc-host addr restore\n' >/etc/local.d/vdc-addrs.start
  chmod 755 /etc/local.d/vdc-addrs.start
  start_docker() { rc-service docker start >/dev/null 2>&1 || true; }
elif command -v apt-get >/dev/null; then
  if ! have_docker; then
    echo "installing docker and docker compose (apt)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q docker.io
    apt-get install -y -q docker-compose-v2 2>/dev/null || apt-get install -y -q docker-compose-plugin
  fi
  cat >/etc/systemd/system/vdc-addrs.service <<'UNIT'
[Unit]
Description=extra addresses of vdc-containers
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/vdc-host addr restore
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable vdc-addrs.service >/dev/null 2>&1
  start_docker() { systemctl enable --now docker >/dev/null 2>&1 || true; }
else
  die "not Alpine, Ubuntu or Debian: install docker and docker compose yourself, then run this again"
fi

# container logs rotate instead of filling the disk
mkdir -p /etc/docker
[ -s /etc/docker/daemon.json ] ||
  printf '{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}\n' >/etc/docker/daemon.json
# a container published on an extra address starts even before that address is back after a reboot
echo "net.ipv4.ip_nonlocal_bind = 1" >/etc/sysctl.d/90-vdc-containers.conf
sysctl -q -w net.ipv4.ip_nonlocal_bind=1 || true
/usr/local/bin/vdc-host addr restore

start_docker
i=0
until docker info >/dev/null 2>&1; do
  i=$((i + 1)); [ $i -lt 60 ] || die "dockerd did not start (see /var/log/docker.log or journalctl -u docker)"
  sleep 1
done
/usr/local/bin/vdc-host check
