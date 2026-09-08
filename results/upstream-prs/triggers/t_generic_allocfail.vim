" Verification only.  The binaries used here are built with an allocation id on
" the alloc() in parse_generic_func_type_args() so that test_alloc_fail() can
" make exactly that allocation fail (aid_generic_name == 35 in this build).
call test_alloc_fail(35, 0, 1)
source t_gen9.vim
qall!
