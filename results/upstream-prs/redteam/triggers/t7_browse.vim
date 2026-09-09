" GTK-built binary run in the terminal: do_browse() gives E338 and returns NULL,
" so the "operation cancelled" return in ex_redir() is reached without any dialog.
silent! browse redir > /tmp/claude-1000/rt7_redir.txt
silent! browse redir >> /tmp/claude-1000/rt7_redir.txt
silent! browse redir > $HOME/rt7_redir_env.txt
redir END
qall!
