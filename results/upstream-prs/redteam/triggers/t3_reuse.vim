vim9script
silent! echo reduce("abc", (acc, c) => [][0])
def Pad(): number
  var a = [1, 2, 3]
  return len(a)
enddef
for i in range(20)
  Pad()
endfor
qall!
