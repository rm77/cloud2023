NAMA_INTERFACE="tap0"
NAMA_INTERFACE_DALAM="x0"
IP_INTERFACE="172.20.20.1/24"
sudo ip tuntap add $NAMA_INTERFACE mode tap
sudo ip addr add $IP_INTERFACE dev $NAMA_INTERFACE
sudo ip link set $NAMA_INTERFACE up


curl --unix-socket ./firecracker.socket -i \
    -X PUT "http://localhost/network-interfaces/$NAMA_INTERFACE_DALAM" \
    -H "accept: application/json" \
    -H "Content-Type: application/json" \
    -d "{
	   \"iface_id\": \"$NAMA_INTERFACE_DALAM\",
	   \"guest_mac\": \"AA:FC:00:00:00:01\",
	   \"host_dev_name\": \"tap0\"
    }"

