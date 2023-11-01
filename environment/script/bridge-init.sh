#!/bin/bash

PARAM=${1-br0}


mkdir $PARAM > /dev/null 2> /dev/null

if [ "$?" -gt 0 ]; then
	echo "nama $PARAM sudah ada "
	exit 200;
fi

cd $PARAM
LOKASI=$(pwd)


cat > .network-settings <<EOF
BRIDGENAME=$PARAM
NETWORK=192.168.44.0 
NETMASK=255.255.255.0 
GATEWAY=192.168.44.1
DHCPRANGE=192.168.44.5,192.168.44.7

EOF

cat > down.sh <<EOF
#!/bin/sh
. $LOKASI/.network-settings
. /script/net-lib/network-util.sh
if test "\$1" ; then
    	do_ifconfig "\$1" down
	ip tuntap del "\$1"
	exit 0
fi
EOF

cat > up.sh <<EOF
#!/bin/sh
. $LOKASI/.network-settings
. /script/net-lib/network-util.sh
if test "\$1" ; then
	do_ifconfig "\$1" 0.0.0.0 up
   	do_brctl addif "\$BRIDGENAME" "\$1"
	exit 0
fi
EOF

cat > create.sh <<EOF
#!/bin/sh
. ./.network-settings
. /script/net-lib/network-util.sh
chmod +x delete-*.sh  > /dev/null 2> /dev/null
./delete-rule.sh  > /dev/null 2> /dev/null
./delete-bridge.sh > /dev/null 2> /dev/null
setup_bridge_nat "\$BRIDGENAME"
EOF

cat >  delete.sh <<EOF
#!/bin/sh
. ./.network-settings
. /script/net-lib/network-util.sh
chmod +x delete-*.sh 2> /dev/null
pid=\$(cat dnsmasq.pid 2> /dev/null)
kill -9 \$pid 2> /dev/null
./delete-bridge.sh > /dev/null 2> /dev/null
./delete-rule.sh > /dev/null 2> /dev/null
EOF



