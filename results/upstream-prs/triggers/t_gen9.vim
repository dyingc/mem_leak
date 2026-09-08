vim9script
# The type argument must be a composite type: type_name() then has to build the
# name in allocated memory and hands it back through "tofree" (ret_free).
def Identity<T>(x: T): T
  return x
enddef
silent! echo Identity<list<number>>([1])
writefile(['reached-call'], '/tmp/claude-1000/gprobe3.txt')
