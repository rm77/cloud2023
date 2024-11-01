docker run \
	-d \
	--name cfy  \
	-v /sys/fs/cgroup:/sys/fs/cgroup:ro --tmpfs /run --tmpfs /run/lock \
	-p 40080:80 cloudifyplatform/community-cloudify-manager-aio:latest
	#-v $(pwd)/config.yaml:/etc/cloudify/config.yaml:rw \
