rm -f ./firecracker.socket
./firecracker \
	--api-sock ./firecracker.socket  \
	--id mesin1 &> /dev/null
echo "$!" > process.pid
