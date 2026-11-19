#!/usr/bin/env bash
# build.sh BASE.qcow2 [IMAGE ...] - bake state/image/docker-alpine.qcow2: Alpine with Docker,
# Docker Compose and vdc-host, plus the container images named (pulled now, so hosts need no
# internet to start them). Needs packer, qemu-system-x86_64, xorriso (or mkisofs), internet.
# Packer keeps its plugins and cache in state/packer, not in your home folder.
#
#   image/build.sh ~/images/alpine-3.22-cloudinit.qcow2 nginx:alpine alpine:3.20
#   sudo ./cluster.sh image add docker-alpine state/image/docker-alpine.qcow2 --user alpine   (in the kit)
#   python3 vdc_containers.py --host-image docker-alpine ...
set -euo pipefail
here=$(dirname "$(readlink -f "$0")")
[ $# -ge 1 ] && [ -f "$1" ] || { echo "usage: $0 BASE.qcow2 [CONTAINER-IMAGE ...]"; exit 1; }
base=$(readlink -f "$1"); shift
command -v packer >/dev/null || { echo "error: packer is not installed"; exit 1; }
st=$(readlink -f "$here/..")/state
PK=$st/packer
mkdir -p "$PK/plugins" "$PK/cache" "$PK/tmp" "$st/image"
export PACKER_PLUGIN_PATH=$PK/plugins PACKER_CACHE_DIR=$PK/cache PACKER_CONFIG_DIR=$PK TMPDIR=$PK/tmp CHECKPOINT_DISABLE=1

# a key for this build only, and a cloud-init seed that lets root in with it
seed=$here/seed
mkdir -p "$seed"
rm -f "$PK/build_key" "$PK/build_key.pub"
ssh-keygen -q -t ed25519 -N "" -C vdc-containers-build -f "$PK/build_key"
printf '{"instance-id": "docker-host-build", "local-hostname": "docker-host"}\n' >"$seed/meta-data"
cat >"$seed/user-data" <<UD
#cloud-config
disable_root: false
ssh_authorized_keys:
  - "$(cat "$PK/build_key.pub")"
UD
trap 'rm -rf "$seed" "$PK/build_key" "$PK/build_key.pub"' EXIT

accel=kvm
[ -w /dev/kvm ] || { accel=tcg; echo "warning: no /dev/kvm, building with TCG (slow)"; }
rm -rf "$st/image/build"
cd "$here"
packer init docker-host.pkr.hcl
packer build -var "base=$base" -var "ssh_key=$PK/build_key" -var "out_dir=$st/image" -var "accel=$accel" \
  -var "preload=$*" docker-host.pkr.hcl
mv "$st/image/build/docker-alpine.qcow2" "$st/image/docker-alpine.qcow2"
rm -rf "$st/image/build"
echo "done: $st/image/docker-alpine.qcow2"
echo "next, in the kit: sudo ./cluster.sh image add docker-alpine $st/image/docker-alpine.qcow2 --user alpine"
