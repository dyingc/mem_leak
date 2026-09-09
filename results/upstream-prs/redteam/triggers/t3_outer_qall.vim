vim9script
delete("/tmp/claude-1000/rt3g.txt")
writefile(["G outer before: depth=" .. test_getvalue("rt_funccal_depth")], "/tmp/claude-1000/rt3g.txt", "a")
source t3_inner_reduce.vim
writefile(["G outer after: depth=" .. test_getvalue("rt_funccal_depth")], "/tmp/claude-1000/rt3g.txt", "a")
qall!
