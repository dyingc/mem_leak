vim9script
def Report(tag: string)
  writefile([tag .. ' funccal_depth=' .. test_getvalue('rt_funccal_depth')], '/tmp/claude-1000/rt3.txt', 'a')
enddef
delete('/tmp/claude-1000/rt3.txt')
Report('baseline')
# closure => VAR_PARTIAL with compiled pt_func (claim: needed)
def MakeClosure(): func
  var x = 1
  return (acc: string, c: string): string => {
    if c == 'b'
      throw 'boom'
    endif
    return acc .. c .. x
  }
enddef
var Cl = MakeClosure()
for i in range(5)
  try
    reduce('abc', Cl)
  catch
  endtry
endfor
Report('string_reduce closure throw x5')
# plain lambda (author says VAR_FUNC, no funccal)
for i in range(5)
  silent! echo reduce('abc', (acc, c) => [][0])
endfor
Report('string_reduce lambda E684 x5')
# same shape on list and blob and tuple and dict: siblings
for i in range(5)
  try
    reduce(['a', 'b', 'c'], Cl)
  catch
  endtry
endfor
Report('list_reduce closure throw x5')
for i in range(5)
  try
    reduce(0z616263, (acc: number, c: number): number => {
      throw 'boom'
    })
  catch
  endtry
endfor
Report('blob_reduce lambda throw x5')
def MakeClosureN(): func
  var x = 1
  return (acc: number, c: number): number => {
    throw 'boom' .. x
  }
enddef
var ClN = MakeClosureN()
for i in range(5)
  try
    reduce(0z616263, ClN)
  catch
  endtry
endfor
Report('blob_reduce closure throw x5')
for i in range(5)
  try
    reduce(('a', 'b', 'c'), Cl)
  catch
  endtry
endfor
Report('tuple_reduce closure throw x5')
# filter/map siblings with closures that throw
def MakeFilterCl(): func
  var x = 1
  return (k: any, v: any): bool => {
    throw 'boom' .. x
  }
enddef
var FCl = MakeFilterCl()
for i in range(5)
  try
    filter('abc', FCl)
  catch
  endtry
endfor
Report('string filter closure throw x5')
for i in range(5)
  try
    filter([1, 2, 3], FCl)
  catch
  endtry
endfor
Report('list filter closure throw x5')
for i in range(5)
  try
    filter({a: 1, b: 2}, FCl)
  catch
  endtry
endfor
Report('dict filter closure throw x5')
for i in range(5)
  try
    filter(0z0102, FCl)
  catch
  endtry
endfor
Report('blob filter closure throw x5')
for i in range(5)
  try
    map((1, 2), FCl)
  catch
  endtry
endfor
Report('tuple map closure throw x5')
# reduce with a string closure that succeeds (control)
var Ok = MakeClosure()
for i in range(5)
  reduce('ac', Ok)
endfor
Report('string_reduce closure ok x5 (control)')
qall!
