func Report(tag)
  call writefile([a:tag . ' live_lists=' . test_getvalue('rt_live_lists')], '/tmp/claude-1000/rt1gc2.txt', 'a')
endfunc
call delete('/tmp/claude-1000/rt1gc2.txt')
for i in range(1000)
  call setmatches([{'group':'Search','id':4,'priority':10,'pos1':[1,1,1],'pos2':[2,1,1]}])
endfor
call Report('after 1000x 2pos')
call garbagecollect()
call Report('after garbagecollect() (flag only)')
call feedkeys(":call Report('after main loop')\<CR>:qall!\<CR>", 'n')
