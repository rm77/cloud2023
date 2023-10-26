NAMA_STORAGE="storage-1.ext4"
dd if=/dev/zero of=$NAMA_STORAGE bs=1M count=10
mkfs.ext4 $NAMA_STORAGE

