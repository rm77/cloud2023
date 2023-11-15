#!/bin/sh
numsession=$(tmux -S /script/.tmux-session ls 2> /dev/null | wc -l)
if [ "$numsession" -eq 0 ]
then
       TMUXCMD=" new -s mysession"
fi

tmux -S /script/.tmux-session $TMUXCMD

