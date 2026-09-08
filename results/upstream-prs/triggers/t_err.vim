call test_alloc_fail(34, 0, 1)
call writefile(['errmsg=' . v:errmsg], '/tmp/claude-1000/gerr.txt')
qall!
