vim9script
def Identity<T>(x: T): T
  return x
enddef
# composite type argument => type_name() allocates (ret_free != NULL)
test_alloc_fail(35, 0, 1)
silent! echo Identity<list<number>>([1])
# simple type argument => static name, nothing to leak (control)
test_alloc_fail(35, 0, 1)
silent! echo Identity<number>(1)
qall!
