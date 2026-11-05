#!/bin/sh
# hub/setup.sh - HUB role: Alpine + libvirtd + the cluster control plane.
#
#   eth0 (uplink, DHCP)  -- NAT --  eth1 (underlay, static HUB_IP)
#   logs: /var/log/lab/{hub,api,dnsmasq,guest}.log
#   dnsmasq    DNS for the underlay (NAME.lab) and the tenant gateways; DHCP too if IP_MODE=dhcp
#   lab-jump   SSH jump host for tenants (JUMP_PORT), keys only, forwarding only
#   nftables   tenant router: NAT out, tenants apart (written by hubctl apply)
#   ttyd       web consoles of guests, proxied by the cloud API
#   lab-hub    br-t<VNI> per tenant + g<VNI>-<node> Geneve tunnel per spoke
#   lab-api    busybox httpd: /cgi-bin/join|state|leave, /images/cirros.qcow2
#   libvirtd   the hub is also a compute node (HUB_SCHEDULABLE)
#   hubctl     CLI for nodes, tenants, guests
#
#   BAKE=1 KIT=/tmp/kit sh hub/setup.sh      (image build, Packer chroot)
#   KIT=/root/vdc-cluster sh hub/setup.sh    (existing Alpine VM, then reboot)
set -eu
KIT=${KIT:-$(cd "$(dirname "$0")/.." && pwd)}
. "$KIT/common/setup-base.sh"

base_install
say "hub packages"
apk add --no-cache dnsmasq busybox-extras ttyd
role_files hub
chmod 755 /usr/local/lib/lab/cgi.sh /var/lib/lab/www/cgi-bin/*
chmod 755 /var/lib/lab /var/lib/lab/www /var/lib/lab/www/cgi-bin

say "API user + the one privileged call it may make"
id labweb >/dev/null 2>&1 || adduser -S -D -H -h /var/lib/lab/www -s /sbin/nologin labweb
mkdir -p /etc/doas.d
echo "permit nopass labweb as root cmd /usr/local/sbin/hubctl" >/etc/doas.d/lab.conf
chmod 600 /etc/doas.d/lab.conf
wrote /etc/doas.d/lab.conf "the API user may run hubctl as root, nothing else"

say "hub: uplink $HUB_UPLINK_IF (DHCP), underlay $HUB_UNDERLAY_IF = $HUB_IP/$UNDERLAY_PREFIX"
cat >/etc/network/interfaces <<NET
auto lo
iface lo inet loopback

auto $HUB_UPLINK_IF
iface $HUB_UPLINK_IF inet dhcp

auto $HUB_UNDERLAY_IF
iface $HUB_UNDERLAY_IF inet static
    address $HUB_IP/$UNDERLAY_PREFIX
    mtu $UNDERLAY_MTU
NET
wrote /etc/network/interfaces "hub: uplink (DHCP) + underlay (static)"
echo hub >/etc/hostname

say "hub: DNS (underlay + tenant gateways), SSH jump host on port ${JUMP_PORT:-45222}, firewall"
/usr/local/sbin/lab-hub-config

say "CirrOS base image, served to every node"
install -d -m 755 /var/lib/lab/www/images
if [ ! -s /var/lib/lab/www/images/cirros.qcow2 ]; then
  if wget -q -O /var/lib/lab/www/images/cirros.qcow2.part "$CIRROS_URL"; then
    mv /var/lib/lab/www/images/cirros.qcow2.part /var/lib/lab/www/images/cirros.qcow2
    chmod 644 /var/lib/lab/www/images/cirros.qcow2
  else
    rm -f /var/lib/lab/www/images/cirros.qcow2.part
    echo "warning: CirrOS download failed; run 'hubctl image' later" >&2
  fi
fi

say "hub services"
svc nftables boot
svc lab-hub default
svc dnsmasq default
svc lab-api default

finish hub
