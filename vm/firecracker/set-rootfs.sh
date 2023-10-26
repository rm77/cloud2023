curl --unix-socket ./firecracker.socket -i \
  -X PUT 'http://localhost/drives/rootfs' \
  -H 'Accept: application/json'           \
  -H 'Content-Type: application/json'     \
  -d "{
        \"drive_id\": \"rootfs\",
        \"path_on_host\": \"hello-rootfs.ext4\",
        \"is_root_device\": true,
        \"is_read_only\": false
   }"
