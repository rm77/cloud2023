# A Kubernetes node image: an Alpine cloud image with k3s, its air-gap images, the MetalLB
# manifest and vdc-node already in it. Nodes made from it install nothing from the internet but
# the MetalLB images (quay.io) and your own application images. Run through build.sh.
packer {
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = ">= 1.1.0"
    }
  }
}

variable "base" {
  type        = string
  description = "the Alpine cloud image (qcow2) to start from, e.g. the file given to cluster.sh image add alpine"
}
variable "ssh_key" {
  type        = string
  description = "private key for this build only (build.sh makes it)"
}
variable "out_dir" {
  type    = string
  default = "../state/image"
}
variable "accel" {
  type    = string
  default = "kvm"
}
variable "k3s_channel" {
  type    = string
  default = "stable"
}
variable "k3s_version" {
  type        = string
  default     = ""
  description = "a fixed k3s version (e.g. v1.33.5+k3s1); empty: the newest of k3s_channel"
}
variable "metallb" {
  type    = string
  default = "v0.16.1"
}

source "qemu" "k3s" {
  iso_url          = var.base
  iso_checksum     = "none"
  disk_image       = true
  use_backing_file = false
  disk_size        = "4G"
  format           = "qcow2"
  accelerator      = var.accel
  memory           = 1024
  cpus             = 2
  headless         = true
  net_device       = "virtio-net"
  disk_interface   = "virtio"
  cd_files         = ["${path.root}/seed/meta-data", "${path.root}/seed/user-data"]
  cd_label         = "cidata"
  communicator     = "ssh"
  ssh_username     = "root"
  ssh_private_key_file = var.ssh_key
  ssh_timeout      = "15m"
  shutdown_command = "poweroff"
  output_directory = "${var.out_dir}/build"
  vm_name          = "k3s-alpine.qcow2"
}

build {
  sources = ["source.qemu.k3s"]

  provisioner "file" {
    source      = "${path.root}/../node/vdc-node"
    destination = "/usr/local/bin/vdc-node"
  }
  provisioner "shell" {
    environment_vars = ["K3S_CHANNEL=${var.k3s_channel}", "K3S_VERSION=${var.k3s_version}", "METALLB=${var.metallb}"]
    inline = [
      "set -eu",
      "chmod 755 /usr/local/bin/vdc-node",
      "apk add -q curl",
      "rc-update add cgroups boot",
      "mkdir -p /usr/local/share/vdc /var/lib/rancher/k3s/agent/images",
      "curl -sfL https://get.k3s.io -o /usr/local/share/vdc/k3s-install.sh",
      "chmod 755 /usr/local/share/vdc/k3s-install.sh",
      # the binary only: node-setup.sh runs the installer again (no download) as server or agent
      "INSTALL_K3S_SKIP_ENABLE=true INSTALL_K3S_SKIP_START=true INSTALL_K3S_CHANNEL=$K3S_CHANNEL ${var.k3s_version != "" ? "INSTALL_K3S_VERSION=$K3S_VERSION" : ""} sh /usr/local/share/vdc/k3s-install.sh",
      "v=$(k3s --version | awk 'NR == 1 {print $3}'); echo \"k3s $v\"",
      "curl -sfL -o /var/lib/rancher/k3s/agent/images/k3s-airgap-images-amd64.tar.zst https://github.com/k3s-io/k3s/releases/download/$(echo $v | sed 's/+/%2B/')/k3s-airgap-images-amd64.tar.zst",
      "curl -sfL -o /usr/local/share/vdc/metallb-native-$METALLB.yaml https://raw.githubusercontent.com/metallb/metallb/$METALLB/config/manifests/metallb-native.yaml",
      "rm -f /etc/init.d/k3s /etc/rancher/k3s/k3s.env",
    ]
  }
  # the guest made from the image runs cloud-init again and gets its own keys and identity
  provisioner "shell" {
    inline = [
      "cloud-init clean --logs --seed || true",
      "rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys /root/.ash_history",
      ": > /etc/machine-id",
      "sync",
    ]
  }
}
