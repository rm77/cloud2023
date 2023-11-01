#!/bin/sh
TFTPROOT=
BOOTP=


generate_setup_vm_image(){
cat <<EOF >user-data.cfg
#cloud-config
instance-id: $VMNAME
local-hostname: host-$VMNAME
hostname: host-$VMNAME
manage_etc_hosts: false
ssh_pwauth: true
disable_root: false
users:
  - default
  - name: ubuntu
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
chpasswd:
  list: |
    root:password
    ubuntu:myubuntu
  expire: false
bootcmd:
- uuidgen | md5sum | cut -d" " -f1 > /etc/machine-id
EOF

cat <<EOF >network-config.cfg
version: 2
ethernets:
    ens3:
      dhcp4: false
      gateway4: 192.168.0.1
      addresses:
      - 192.168.0.100/24
      nameservers:
          search: [its.ac.id]
          addresses: [8.8.8.8] 
EOF
   	
}

update_vm_image(){
	set -x
	MODE=$1 || "update"

	echo $MODE

	if [ "$MODE" = "new" ]; then
	   #setup disk image
	   rm -f $VMNAME.qcow2
	   rm -f cloud-init.iso
	fi

	qemu-img convert -f qcow2 -O qcow2 $VMIMAGELOCATION $VMNAME.qcow2
	qemu-img resize $VMNAME.qcow2 $VMSIZE
	qemu-img info $VMNAME.qcow2


	#setup data
	rm -f cloud-init.iso
	#jika ingin menggunakan static IP
	
	#harus mengaktifkan network-config 
	#cloud-localds -v -m local --network-config=network-config.cfg cloud-init.iso user-data.cfg 

	#menggunakan dynamic IP address via DHCP
	cloud-localds -v -m local cloud-init.iso user-data.cfg 

	#hapus config
	#rm -f user-data.cfg network-config.cfg
}

generate_start_vm(){
   set -x

   rm -f net-up.sh
   rm -f net-down.sh
   ln -s ../$NETBRIDGE/up.sh net-up.sh
   ln -s ../$NETBRIDGE/down.sh net-down.sh
   chmod +x ../$NETBRIDGE/up.sh
   chmod +x ../$NETBRIDGE/down.sh



   vncport=$((5900+$VMVNCPORT))
cat <<EOF > create.sh
   qemu-system-x86_64 \\
     -daemonize  \\
     -enable-kvm \\
     -name $VMNAME \\
     -m $VMMEMORY \\
     -smp $VMCPU \\
     -drive file=$VMNAME.qcow2,if=virtio \\
     -drive file=cloud-init.iso,media=cdrom,if=virtio \\
     -monitor telnet:0.0.0.0:$VMMONITORPORT,server,nowait,nodelay \\
     -serial telnet:0.0.0.0:$VMSERIALPORT,server,nowait,nodelay \\
     -vnc :$VMVNCPORT,password=on \\
     -netdev tap,id=interface0,ifname=$VMNAME-tap0,script=net-up.sh,downscript=net-down.sh \\
     -device virtio-net-pci,netdev=interface0,mac=$VMMACADDRESS \\
     -pidfile $VMNAME.pid 

    sleep 1

    echo set_password vnc $VMVNCPASSWORD | nc -q 1 0.0.0.0 $VMMONITORPORT 
    echo expire_password vnc never | nc -q 1 0.0.0.0 $VMMONITORPORT 

    #vnc jalan di port $vncport

    ln -s /opts/noVNC/vnc.html /opts/noVNC/index.html
    nohup /opts/noVNC/utils/novnc_proxy --vnc 0.0.0.0:$vncport --listen $VMWEBVNCPORT &
    echo \$! > WEB$VMNAME.pid
    echo \$\$ > WEB$VMNAME.pgid
    sleep 1
    ppid=\$(pgrep -f websockify )
    echo \$ppid >> WEB$VMNAME.pid

    echo MONITORPORT=$VMMONITORPORT
    echo VMSERIALPORT=$VMSERIALPORT
    echo VNCPORT=$vncport
    echo WEBVNCPORT=$VMWEBVNCPORT
    echo VNCPASSWORD=$VMVNCPASSWORD
EOF

}

