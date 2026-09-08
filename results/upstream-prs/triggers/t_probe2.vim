vim9script
var log: list<string> = []
add(log, 'af=' .. string(test_alloc_fail(34, 0, 1)))
try
  def Identity<T>(x: T): T
    return x
  enddef
  add(log, 'defined')
  add(log, 'call=' .. string(Identity<number>(1)))
catch
  add(log, 'ERR ' .. v:exception)
endtry
writefile(log, '/tmp/claude-1000/gprobe2.txt')
qall!
