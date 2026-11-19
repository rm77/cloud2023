# A container host image: an Alpine cloud image with Docker, Docker Compose and vdc-host
# already in it, and optionally some container images pulled. Hosts made from it start
# without installing anything, so tenants need no internet for that. Run through build.sh.
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
variable "preload" {
  type        = string
  default     = ""
  description = "container images to pull into the image, space-separated (e.g. 'nginx:alpine alpine:3.20')"
}

source "qemu" "docker" {
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
  vm_name          = "docker-alpine.qcow2"
}

build {
  sources = ["source.qemu.docker"]

  provisioner "file" {
    source      = "${path.root}/../host/vdc-host"
    destination = "/usr/local/bin/vdc-host"
  }
  provisioner "shell" {
    inline = ["chmod 755 /usr/local/bin/vdc-host"]
  }
  provisioner "shell" {
    script = "${path.root}/../host/host-setup.sh"
  }
  provisioner "shell" {
    inline = ["for i in ${var.preload}; do echo \"pulling $i\"; docker pull -q \"$i\"; done"]
  }
  # the guest made from the image runs cloud-init again and gets its own keys and identity
  provisioner "shell" {
    inline = [
      "rc-service docker stop || true",
      "cloud-init clean --logs --seed || true",
      "rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys /root/.ash_history",
      ": > /etc/machine-id",
      "sync",
    ]
  }
}
