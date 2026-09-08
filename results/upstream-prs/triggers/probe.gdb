set pagination off
set confirm off
break parse_generic_func_type_args
break vim9generics.c:327
run --not-a-term -u NONE -i NONE -es -S t_generic_allocfail.vim
bt 3
info locals
continue
