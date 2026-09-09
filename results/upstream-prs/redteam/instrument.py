#!/usr/bin/env python3
"""Red-team probes, applied identically to orig and fixed trees.
Does NOT touch any line the 9 fix diffs modify (only lines they use as context),
so it is applied AFTER the fixes."""
import sys, os
src = sys.argv[1]
def sub(path, old, new, count=1):
    p = os.path.join(src, path); s = open(p).read()
    assert s.count(old) == count, (path, old, s.count(old))
    open(p, "w").write(s.replace(old, new))

# 1. alloc ids for deterministic failure injection via test_alloc_fail()
sub("alloc.h", "    aid_last", "    aid_rt_generic_name,\n    aid_rt_class_name,\n    aid_rt_gtk_conv,\n    aid_last")
sub("vim9generics.c", "generic_arg->gt_name = alloc(STRLEN(ret_name) + 1);",
    "generic_arg->gt_name = alloc_id(STRLEN(ret_name) + 1, aid_rt_generic_name);")
sub("vim9class.c", "cl->class_name.string = vim_strnsave(name_start, cl->class_name.length);",
    "cl->class_name.string = (alloc_fail_id == aid_rt_class_name && alloc_does_fail(cl->class_name.length + 1)) ? NULL : vim_strnsave(name_start, cl->class_name.length);")
sub("gui_gtk_x11.c", "new_conv_buf = alloc(convlen + 2);", "new_conv_buf = alloc_id(convlen + 2, aid_rt_gtk_conv);")

# 2. live list_T counter (LSAN is blind to lists: first_list chain keeps them reachable)
sub("list.c", "static list_T\t\t*first_list = NULL;", "long rt_live_lists = 0;\nstatic list_T\t\t*first_list = NULL;")
sub("list.c", "    l = ALLOC_CLEAR_ONE(list_T);\n    if (l != NULL)\n\tlist_init(l);\n    return l;",
    "    l = ALLOC_CLEAR_ONE(list_T);\n    if (l != NULL)\n    {\n\tlist_init(l);\n\t++rt_live_lists;\n    }\n    return l;")
sub("list.c", "    l = (list_T *)alloc_clear(sizeof(list_T) + count * sizeof(listitem_T));\n    if (l == NULL)\n\treturn NULL;\n\n    list_init(l);",
    "    l = (list_T *)alloc_clear(sizeof(list_T) + count * sizeof(listitem_T));\n    if (l == NULL)\n\treturn NULL;\n\n    list_init(l);\n    ++rt_live_lists;")
sub("list.c", "    free_type(l->lv_type);\n    vim_free(l);\n}", "    free_type(l->lv_type);\n    --rt_live_lists;\n    vim_free(l);\n}")

# 3. funccall_T chain depth (current_funccal is static in userfunc.c)
sub("userfunc.c", "static funccall_T *current_funccal = NULL;",
    "static funccall_T *current_funccal = NULL;\n    long\nrt_funccal_depth(void)\n{\n    long n = 0;\n    funccall_T *fc;\n    for (fc = current_funccal; fc != NULL; fc = fc->fc_caller)\n\t++n;\n    return n;\n}")

# 4. expose through test_getvalue()
sub("testing.c", '    if (STRCMP(name, (char_u *)"need_fileinfo") == 0)\n\trettv->vval.v_number = need_fileinfo;',
    '    extern long rt_live_lists;\n    extern long rt_funccal_depth(void);\n    if (STRCMP(name, (char_u *)"need_fileinfo") == 0)\n\trettv->vval.v_number = need_fileinfo;\n    else if (STRCMP(name, (char_u *)"rt_live_lists") == 0)\n\trettv->vval.v_number = rt_live_lists;\n    else if (STRCMP(name, (char_u *)"rt_funccal_depth") == 0)\n\trettv->vval.v_number = rt_funccal_depth();')
print("instrumented", src)
