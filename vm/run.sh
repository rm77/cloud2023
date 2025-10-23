#!/bin/bash

DOCKERNAME=vn1
NETNAME=my_network
SUBNET=172.16.254.0/24
GATEWAY=172.16.254.1
IPRANGE=172.16.254.20,172.16.254.25
docker network create --driver=bridge --subnet=${SUBNET} --gateway=${GATEWAY} $NETNAME  
docker rm -f $DOCKERNAME
docker run -d --name $DOCKERNAME \
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

