#!/bin/sh
#
# Copyright IBM, Corp. 2010  
#
# Authors:
#  Anthony Liguori <aliguori@us.ibm.com>
#
# This work is licensed under the terms of the GNU GPL, version 2.  See
# the COPYING file in the top-level directory.

do_brctl() {
    brctl "$@"
}

do_ifconfig() {
    ifconfig "$@"
}

do_dnsmasq() {
    dnsmasq "$@"
}

check_bridge() {
    if [ $(do_brctl show | grep "^$1" | wc -l ) -gt 0 ] ; then
        echo 1
    else
	echo 0
    fi
}

create_bridge() {
    do_brctl addbr "$1"
    do_brctl stp "$1" off
    do_brctl setfd "$1" 0
    do_ifconfig "$1" "$GATEWAY" netmask "$NETMASK" up
    echo "ip link del $1 " > delete-bridge.sh
}


enable_ip_forward() {
    echo 1 > /proc/sys/net/ipv4/ip_forward
}

add_filter_rules() {

chainname=FW-$BRIDGENAME

iptables -t nat -N $chainname-POSTROUTING
iptables -t nat -A $chainname-POSTROUTING  -s $NETWORK/$NETMASK -j MASQUERADE 
iptables -t nat -A POSTROUTING -j $chainname-POSTROUTING

echo iptables -t nat -D POSTROUTING -j $chainname-POSTROUTING > delete-rule.sh
echo iptables -t nat -F $chainname-POSTROUTING >> delete-rule.sh
echo iptables -t nat -X $chainname-POSTROUTING >> delete-rule.sh

iptables -N $chainname-INPUT
iptables -A $chainname-INPUT  -i $BRIDGENAME -p tcp -m tcp --dport 67 -j ACCEPT 
iptables -A $chainname-INPUT  -i $BRIDGENAME -p udp -m udp --dport 67 -j ACCEPT 
iptables -A $chainname-INPUT  -i $BRIDGENAME -p tcp -m tcp --dport 53 -j ACCEPT 
iptables -A $chainname-INPUT  -i $BRIDGENAME -p udp -m udp --dport 53 -j ACCEPT 
iptables -A INPUT -j $chainname-INPUT

echo iptables -D INPUT -j $chainname-INPUT >> delete-rule.sh
echo iptables -F $chainname-INPUT >> delete-rule.sh
echo iptables -X $chainname-INPUT >> delete-rule.sh


iptables -N $chainname-FORWARD
iptables -A $chainname-FORWARD -i $1 -o $1 -j ACCEPT 
iptables -A $chainname-FORWARD -s $NETWORK/$NETMASK -i $BRIDGENAME -j ACCEPT 
iptables -A $chainname-FORWARD -d $NETWORK/$NETMASK -o $BRIDGENAME -m state --state RELATED,ESTABLISHED -j ACCEPT 
iptables -A $chainname-FORWARD -o $BRIDGENAME -j REJECT --reject-with icmp-port-unreachable 
iptables -A $chainname-FORWARD -i $BRIDGENAME -j REJECT --reject-with icmp-port-unreachable 
iptables -A FORWARD -j $chainname-FORWARD

echo iptables -D FORWARD -j $chainname-FORWARD >> delete-rule.sh
echo iptables -F $chainname-FORWARD >> delete-rule.sh
echo iptables -X $chainname-FORWARD >> delete-rule.sh



}

start_dnsmasq() {
    do_dnsmasq \
	--strict-order \
	--except-interface=lo \
	--interface=$BRIDGENAME \
	--listen-address=$GATEWAY \
	--bind-interfaces \
	--dhcp-range=$DHCPRANGE \
	--conf-file="" \
	--pid-file=$(pwd)/dnsmasq.pid \
	--dhcp-leasefile=$(pwd)/dnsmasq.leases \
	--dhcp-no-override \
	${TFTPROOT:+"--enable-tftp"} \
	${TFTPROOT:+"--tftp-root=$TFTPROOT"} \
	${BOOTP:+"--dhcp-boot=$BOOTP"}
	echo "kill -9 \$(cat dnsmasq.pid)" >> delete-bridge.sh
}

setup_bridge_nat() {
	if [ $(check_bridge "$1") -lt 1 ] ; then
	create_bridge "$1"
	enable_ip_forward
	add_filter_rules "$1"
	start_dnsmasq "$1"
	echo "create"
    fi
}



