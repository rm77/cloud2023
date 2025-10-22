FROM alpine:3.20

RUN apk update && apk add   qemu-system-x86_64  vim tmux novnc docker iproute2 iptables cloud-utils cloud-utils-localds mc htop bridge-utils supervisor
RUN adduser  -G qemu -s /bin/sh -D alpine \
    && echo "alpine:alpine" | /usr/sbin/chpasswd \
    && echo "alpine    ALL=(ALL) ALL" >> /etc/sudoers
COPY supervisord.conf /tmp/supervisord.conf
COPY entrypoint.sh /tmp/entrypoint.sh
CMD ["/bin/sh","/tmp/entrypoint.sh"]
