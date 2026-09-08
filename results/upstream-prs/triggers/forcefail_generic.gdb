set pagination off
set confirm off
break vim9generics.c:327
commands
  silent
  set var generic_arg->gt_name = 0
  continue
end
run --not-a-term -u NONE -i NONE -es -S t_generic.vim
