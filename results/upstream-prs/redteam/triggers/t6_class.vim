vim9script
test_alloc_fail(36, 0, 1)
silent! class Foo
endclass
# the set_var_const() FAIL path: does cleanup double free / leak?
const Bar = 1
silent! class Bar
endclass
silent! class Bar
endclass
qall!
