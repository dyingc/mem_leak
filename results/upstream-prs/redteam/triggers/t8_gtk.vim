" GUI is up (sourced after gui_mch_init).  Switch 'encoding' to a DBCS
" encoding so output_conv (encoding -> utf-8) is active, then show a
" character that is double-wide in euc-jp but single-wide in UTF-8 (ambiguous
" width, 'ambiwidth' single): Greek alpha, euc-jp bytes A6 A1.
set encoding=euc-jp
call setline(1, "\xa6\xa1 alpha")
redraw!
call writefile(['enc=' . &enc . ' tenc=' . &tenc . ' gui=' . has('gui_running')], '/tmp/claude-1000/rt8.txt')
" now make the very next alloc at the conv-buffer site fail
call test_alloc_fail(37, 0, 1)
redraw!
qall!
