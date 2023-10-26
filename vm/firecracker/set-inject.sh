sudo mkdir -p /tmp/myrootfs
sudo mount -t ext4 -o loop hello-rootfs.ext4 /tmp/myrootfs
sudo cat kunci.pub > /tmp/myrootfs/root/.ssh/authorized_keys


sudo cat <<EOF > /tmp/myrootfs/etc/network/interfaces
iface eth0 inet static
address 172.20.20.200
netmask 255.255.255.0
gateway 172.20.20.1
EOF
sudo umount /tmp/myrootfs
