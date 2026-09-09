vim9script
silent! echo reduce("abc", (acc, c) => [][0])
writefile(["G inner: depth=" .. test_getvalue("rt_funccal_depth")], "/tmp/claude-1000/rt3g.txt", "a")
