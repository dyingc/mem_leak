vim9script
test_alloc_fail(35, 0, 1)
def Identity<T>(x: T): T
  return x
enddef
try
  echo Identity<number>(1)
catch
  echo 'caught: ' .. v:exception
endtry
qall!
