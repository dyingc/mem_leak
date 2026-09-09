set viminfofile=/tmp/claude-1000/junk.viminfo
" bar line type 1 is BARTYPE_VERSION; use a history entry instead: type 2 = BARTYPE_HISTORY
" |2,1,0,"cmd"  -> cmdline history entry
rviminfo! good_hist.viminfo
call writefile(['hist=' . string(histget(':', -1))], '/tmp/claude-1000/rt2.txt')
qall!
