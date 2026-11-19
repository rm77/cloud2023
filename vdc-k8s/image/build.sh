#!/usr/bin/env bash
# build.sh BASE.qcow2 - bake state/image/k3s-alpine.qcow2: Alpine with k3s, its air-gap images,
# the MetalLB manifest and vdc-node. Settings: K3S_CHANNEL (stable), K3S_VERSION (a fixed one),
# METALLB (v0.16.1). Needs packer, qemu-system-x86_64, xorriso (or mkisofs), internet.
# Packer keeps its plugins and cache in state/packer, not in your home folder.
#
#   image/build.sh ~/images/alpine-3.22-cloudinit.qcow2
#   sudo ./cluster.sh image add k3s-alpine state/image/k3s-alpine.qcow2 --user alpine   (in the kit)
#   python3 vdc_k8s.py --node-image k3s-alpine ...
set -euo pipefail
here=$(dirname "$(readlink -f "$0")")
[ $# -ge 1 ] && [ -f "$1" ] || { echo "usage: $0 BASE.qcow2"; exit 1; }
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
ssh-keygen -q -t ed25519 -N "" -C vdc-k8s-build -f "$PK/build_key"
printf '{"instance-id": "k3s-node-build", "local-hostname": "k3s-node"}\n' >"$seed/meta-data"
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
packer init k3s-node.pkr.hcl
packer build -var "base=$base" -var "ssh_key=$PK/build_key" -var "out_dir=$st/image" -var "accel=$accel" \
  -var "k3s_channel=${K3S_CHANNEL:-stable}" -var "k3s_version=${K3S_VERSION:-}" -var "metallb=${METALLB:-v0.16.1}" k3s-node.pkr.hcl
mv "$st/image/build/k3s-alpine.qcow2" "$st/image/k3s-alpine.qcow2"
rm -rf "$st/image/build"
echo "done: $st/image/k3s-alpine.qcow2"
echo "next, in the kit: sudo ./cluster.sh image add k3s-alpine $st/image/k3s-alpine.qcow2 --user alpine"
