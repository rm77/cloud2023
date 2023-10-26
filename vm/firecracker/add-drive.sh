NAMA_DRIVE="volume1"
NAMA_STORAGE="storage-1.ext4"
curl --unix-socket ./firecracker.socket -i \
  -X PUT 'http://localhost/drives/'$NAMA_DRIVE \
  -H 'Accept: application/json'           \
  -H 'Content-Type: application/json'     \
  -d "{
        \"drive_id\": \"$NAMA_DRIVE\",
        \"path_on_host\": \"$NAMA_STORAGE\",
	\"is_root_device\": false,
	\"is_read_only\": false
   }"
