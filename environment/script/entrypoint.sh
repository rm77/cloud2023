#ttyd -p 10000 -W -c $AUTH /bin/sh /script/start-tmux.sh
LOC=/script/.tmux-session
SES=mysession
SSHPORT=${PORTSSH:-10022}
TTYDPORT=${PORTTTYD:-10000}
export SSHPORT TTYDPORT
tmux -S $LOC new -s $SES  -d '/bin/bash'
tmux -S $LOC set -w -g mouse 
tmux -S $LOC send-keys 'echo "ubuntu:ubuntu" | chpasswd ; sshd -D -p $SSHPORT -f /etc/ssh/sshd_config ' Enter
tmux -S $LOC split-window -h
tmux -S $LOC send-keys 'ttyd -p $TTYDPORT -W -c $AUTH /bin/sh /script/start-tmux.sh' Enter
tmux -S $LOC new-window
tmux -S $LOC send-keys '/bin/sh /script/start-libvirtd.sh' Enter
tmux -S $LOC split-window -h
tmux -S $LOC send-keys 'virtlogd --daemon;virtlockd --daemon;/bin/bash /script/start-libvirtd-support.sh' Enter
tmux -S $LOC new-window
while true;do sleep 10000;done
