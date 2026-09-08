vim9script
# A Vim9 generic function; parse_generic_func_type_args() runs while compiling
# the declaration below.
def Identity<T>(x: T): T
  return x
enddef
echo Identity<number>(1)
sleep 30
