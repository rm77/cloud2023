docker run \
	-d \
	--privileged \
	--name cfy  \
	--cap-add=SYS_RAWIO \
	--cap-add=ALL \
	-v /sys/fs/cgroup:/sys/fs/cgroup:ro --tmpfs /run --tmpfs /run/lock \
	-p 30080:80 cloudifyplatform/community-cloudify-manager-aio:latest
	#-v $(pwd)/config.yaml:/etc/cloudify/config.yaml:rw \
