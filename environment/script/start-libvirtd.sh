echo "listen_tls = 0" >> /etc/libvirt/libvirtd.conf
echo 'listen_tcp = 1' >> /etc/libvirt/libvirtd.conf
echo 'tls_port = "16514"' >> /etc/libvirt/libvirtd.conf 
echo 'tcp_port = "16509"' >> /etc/libvirt/libvirtd.conf
echo 'auth_tcp = "none"' >> /etc/libvirt/libvirtd.conf
echo 'vnc_listen = "0.0.0.0"' >> /etc/libvirt/qemu.conf
libvirtd --listen 
