#!/bin/bash

NETNAME=my_network
docker network create $NETNAME 
docker rm -f vn1
docker run -d --name vn1 \
        --restart=always \
        --privileged \
        --cap-add=ALL \
        --cap-add=SYS_RAWIO \
        --device /dev/mem:/dev/mem \
        --device /dev/kvm:/dev/kvm \
        --device /dev/shm:/dev/shm \
        --device /dev/pts:/dev/pts \
        -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
        -v /dev/kvm:/dev/kvm \
        -v /dev/shm:/dev/shm \
        -v /dev/pts:/dev/pts \
	-v $(pwd)/ops:/ops \
	-v $(pwd)/scripts:/scripts \
	-v $(pwd)/images:/images \
	-e IMAGE=/images/jammy-server-cloudimg-amd64.img \
        -p 25400:25400 \
        -p 9999:6666 \
	--network $NETNAME \
        vm-dev:1.00

