#!/bin/bash

VM_SIZE=${VM_SIZE:-5G}
VM_NAME=${VM_NAME:-COBA1}
VM_MEMORY=${VM_MEMORY:-2G}
VM_CPU=${VM_CPU:-2}

cat <<EOF > /ops/user-data.cfg
#cloud-config
hostname: host-${VM_NAME}
manage_etc_hosts: false
ssh_pwauth: true
disable_root: false
users:
- default
- name: royyana
  shell: /bin/bash
  sudo: ALL=(ALL) NOPASSWD:ALL
  lock_passwd: false
  plain_text_passwd: "kucinglucu"
bootcmd:
- uuidgen | md5sum | cut -d" " -f1 > /etc/machine-id
EOF

IMAGEFILE=${IMAGE}.qcow2
#qemu-img convert -f qcow2 -O qcow2 ${IMAGE} ${IMAGEFILE}
#qemu-img resize ${IMAGEFILE} ${VM_SIZE}
#qemu-img info ${IMAGEFILE}


rm -f /ops/cloud-init.iso
cloud-localds -v -m local /ops/cloud-init.iso /ops/user-data.cfg 


qemu-system-x86_64 \
        -enable-kvm \
        -name ${VM_NAME} \
        -m ${VM_MEMORY} \
        -smp ${VM_CPU} \
        -drive file=${IMAGE},if=virtio \
        -drive file=/ops/cloud-init.iso,media=cdrom,if=virtio \
	-serial mon:stdio \
	-netdev tap,id=interface0,ifname=tap0,script=/scripts/net-up.sh,downscript=/scripts/net-down.sh \
	-device virtio-net-pci,netdev=interface0 \
        -pidfile /ops/${VM_NAME}.pid
