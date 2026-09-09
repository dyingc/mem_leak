vim9script
def Report(tag: string)
  writefile([tag .. ' funccal_depth=' .. test_getvalue('rt_funccal_depth')], '/tmp/claude-1000/rt3d.txt', 'a')
enddef
delete('/tmp/claude-1000/rt3d.txt')
Report('baseline')
def Thrower(acc: string, c: string): string
  throw 'boom'
enddef
for i in range(5)
  try
    reduce('abc', Thrower)
  catch
  endtry
endfor
Report('string_reduce def-funcref (VAR_FUNC) throw x5')
for i in range(5)
  try
    reduce('abc', function(Thrower))
  catch
  endtry
endfor
Report('string_reduce function(Def) throw x5')
for i in range(5)
  try
    reduce('abc', function(Thrower, []))
  catch
  endtry
endfor
Report('string_reduce partial-of-def throw x5')
# legacy-script lambda, from a legacy function
qall!
