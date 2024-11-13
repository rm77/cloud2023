#/bin/bash
chmod 666 /dev/kvm

cat <<EOF > /tmp/network.xml
<network>
  <name>default</name>
  <forward mode='nat'>
    <nat>
      <port start='1024' end='65535'/>
    </nat>
  </forward>
  <bridge name='virbr2' stp='on' delay='0'/>
  <domain name='default'/>
  <ip address='192.168.100.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.100.128' end='192.168.100.254'/>
    </dhcp>
  </ip>
</network>
EOF

ip link set virbr2 down
brctl delbr virbr2

virsh net-define /tmp/network.xml
virsh net-start default

virsh pool-define-as default dir --target "/home/work/libvirt/images"
virsh pool-build default
virsh pool-start default
virsh pool-autostart default
