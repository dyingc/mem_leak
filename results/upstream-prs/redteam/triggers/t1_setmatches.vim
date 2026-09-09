func Report(tag)
  call writefile([a:tag . ' live_lists=' . test_getvalue('rt_live_lists')], '/tmp/claude-1000/rt1.txt', 'a')
endfunc
call delete('/tmp/claude-1000/rt1.txt')
call Report('baseline')
" (a) normal path, 1 position
for i in range(1000)
  call setmatches([{'group':'Search','id':4,'priority':10,'pos1':[1,1,1]}])
endfor
call Report('after 1000x 1pos')
" (a) normal path, 2 positions
for i in range(1000)
  call setmatches([{'group':'Search','id':4,'priority':10,'pos1':[1,1,1],'pos2':[2,1,1]}])
endfor
call Report('after 1000x 2pos')
" (a) normal path, 3 positions
for i in range(1000)
  call setmatches([{'group':'Search','id':4,'priority':10,'pos1':[1,1,1],'pos2':[2,1,1],'pos3':[3,1,1]}])
endfor
call Report('after 1000x 3pos')
" (b) error path: pos2 not a list
for i in range(1000)
  call setmatches([{'group':'Search','id':4,'priority':10,'pos1':[1,1,1],'pos2':'notalist'}])
endfor
call Report('after 1000x pos2=string')
" (b') error path: pos1 not a list
for i in range(1000)
  call setmatches([{'group':'Search','id':4,'priority':10,'pos1':'notalist'}])
endfor
call Report('after 1000x pos1=string')
" does an explicit GC reclaim them?
call test_garbagecollect_now()
call Report('after test_garbagecollect_now')
" sanity: do matches still work after the fix (behaviour check)
call clearmatches()
call setmatches([{'group':'Search','id':4,'priority':10,'pos1':[1,1,1],'pos2':[2,1,1]}])
call writefile(['getmatches=' . string(getmatches())], '/tmp/claude-1000/rt1.txt', 'a')
call clearmatches()
call test_garbagecollect_now()
call Report('final')
qall!
