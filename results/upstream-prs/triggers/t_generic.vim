vim9script
# Compiling this declaration runs parse_generic_func_type_args().
def Identity<T>(x: T): T
  return x
enddef
echo Identity<number>(1)
qall!
