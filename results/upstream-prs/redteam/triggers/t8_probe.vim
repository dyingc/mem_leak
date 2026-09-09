set encoding=euc-jp
call setline(1, "\xa6\xa1 alpha")
redraw!
let s:l = ['enc=' . &enc . ' tenc=' . &tenc . ' gui=' . has('gui_running') . ' ambw=' . &ambiwidth . ' iconv=' . has('iconv')]
call add(s:l, 'cells=' . strdisplaywidth(getline(1)))
call add(s:l, 'utf8 form=' . iconv(getline(1), 'euc-jp', 'utf-8'))
let v:errmsg = ''
call test_alloc_fail(37, 0, 1)
redraw!
call add(s:l, 'errmsg after armed redraw: ' . v:errmsg)
" second attempt: force a real change so the line is redrawn
call setline(1, "\xa6\xa1 beta")
call test_alloc_fail(37, 0, 1)
redraw!
call add(s:l, 'errmsg after 2nd armed redraw: ' . v:errmsg)
call writefile(s:l, '/tmp/claude-1000/rt8p.txt')
qall!
