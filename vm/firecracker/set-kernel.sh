curl --unix-socket ./firecracker.socket -i \
  -X PUT 'http://localhost/boot-source' \
  -H 'Accept: application/json'           \
  -H 'Content-Type: application/json'     \
  -d "{
        \"kernel_image_path\": \"hello-vmlinux.bin\",
	\"boot_args\": \"console=ttyS0 noapic reboot=k panic=1 pci=off nomodules rw\"
   }"
