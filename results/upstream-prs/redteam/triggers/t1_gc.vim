let v:testing = 1
func Report(tag)
  call writefile([a:tag . ' live_lists=' . test_getvalue('rt_live_lists')], '/tmp/claude-1000/rt1gc.txt', 'a')
endfunc
call delete('/tmp/claude-1000/rt1gc.txt')
call Report('baseline')
for i in range(1000)
  call setmatches([{'group':'Search','id':4,'priority':10,'pos1':[1,1,1],'pos2':[2,1,1]}])
endfor
call Report('after 1000x 2pos')
call test_garbagecollect_now()
call Report('after test_garbagecollect_now (v:testing=1)')
for i in range(1000)
  call setmatches([{'group':'Search','id':4,'priority':10,'pos1':[1,1,1],'pos2':'notalist'}])
endfor
call Report('after 1000x pos2=string')
call test_garbagecollect_now()
call Report('after test_garbagecollect_now (v:testing=1)')
qall!
