# Upstream ground truth: leak fixes in v9.2.0015..FETCH_HEAD

60 leak-related commits (test-only commits excluded).

| patch | subject | changed functions | codeql | codeql-vanilla | infer-argn | infer-cdb | infer-refA | infer-refE | infer-refF | infer-shim2 | infer-vanilla |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 7ed37dc5 | patch memory leak in list_extend_func() in list.c | `list_extend_func` | ✅ |  | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0055 | memory leak in ExpandFromContext() | `ExpandFromContext` | ✅ | ✅ |  |  |  |  |  |  |  |
| 9.2.0056 | memory leak in ex_substitute | `ex_substitute` | ✅ | ✅ |  |  |  |  |  |  |  |
| 9.2.0057 | memory leak in exe_newdict() | `exe_newdict` | ✅ | ✅ | ✅ | ✅ | ✅ |  | ✅ | ✅ |  |
| 9.2.0059 | memory leak in fill_assert_error | `fill_assert_error` |  |  |  |  |  |  |  |  |  |
| 9.2.0063 | memory leak in type_name_list_or_dict() | `type_name_list_or_dict` |  |  |  |  |  |  |  |  |  |
| 9.2.0065 | memory leak in invoke_sync_listeners() | `invoke_sync_listeners` | ✅ |  |  |  |  |  |  |  |  |
| 9.2.0066 | memory leak in build_drop_cmd() | `build_drop_cmd` | ✅ | ✅ |  |  |  |  |  |  |  |
| 9.2.0067 | memory leak in dict_extend_func() | `dict_extend_func` | ✅ |  | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0079 | memory leak in eval_dict() | `eval_dict` | ✅ |  |  |  |  |  |  |  |  |
| 9.2.0097 | Memory leak in qf_push_dir() | `qf_push_dir` |  |  |  |  |  |  |  |  |  |
| 9.2.0102 | 'listchars' "leadtab" not used in :list | `msg_prt_line, set_chars_option` |  |  |  |  |  |  |  |  |  |
| 9.2.0105 | memory leak in heredoc_get() in src/evalvars.c | `heredoc_get` | ✅ |  | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0106 | memory leak in expand_findfunc() | `expand_findfunc` | ✅ |  | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0118 | memory leak in w_hl when reusing a popup window | `win_init_empty` |  |  |  |  |  |  |  |  |  |
| 9.2.0133 | memory leak in netbeans_file_activated() | `netbeans_file_activated` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0134 | memory leak in socket_server_send_reply() | `socket_server_send_reply` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0135 | memory leak in eval_tuple() | `eval_tuple` | ✅ |  |  |  |  |  |  |  |  |
| 9.2.0136 | memory leak in add_interface_from_super_class() | `add_interface_from_super_class` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0200 | term: DECRQM codes are sent too early | `starttermcap, term_replace_keycodes, vim_main2` |  |  |  |  |  |  |  |  |  |
| 9.2.0227 | MS-Windows: CSI sequences may be written to screen | `src/os_win32.c, src/version.c` |  |  |  |  |  |  |  |  |  |
| 9.2.0242 | memory leak in check_for_cryptkey() | `check_for_cryptkey` |  |  |  |  |  | ✅ |  |  |  |
| 9.2.0243 | memory leak in change_indent() | `change_indent` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0244 | memory leak in eval8() | `eval8` | ✅ |  | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0246 | memory leak in globpath() | `globpath` |  |  |  |  |  |  |  |  |  |
| 9.2.0258 | memory leak in add_mark() | `add_mark` | ✅ |  |  |  |  |  |  |  |  |
| 9.2.0268 | memory leak in call_oc_method() | `call_oc_method` |  |  |  |  |  |  |  |  |  |
| 9.2.0290 | Amiga: no support for AmigaOS 3.x | `get_fib, mch_FullName, mch_delay, mch_get_host_name, mch_init, sortcmp` |  |  |  |  |  |  |  |  |  |
| 9.2.0409 | memory leaks in copy_substring_from_pos() | `copy_substring_from_pos` |  |  |  |  |  |  |  |  |  |
| 9.2.0427 | popup: opacity blend may leaks white bg color | `blend_colors, cterm_color_to_rgb, hl_blend_attr, hl_combine_attr, hl_pum_blend_a` |  |  |  |  |  |  |  |  |  |
| 9.2.0441 | statusline: click handler not called on multi-line statusline | `win_redr_custom` |  |  |  |  |  |  |  |  |  |
| 9.2.0476 | pattern completion leaks memory on alloc failures | `copy_substring_from_pos, expand_pattern_in_buf, fuzzy_match_str_with_pos` | ✅ | ✅ |  |  |  |  |  |  |  |
| 9.2.0525 | spell: memory leak in spell_read_dic() | `spell_read_dic` |  |  |  |  |  |  |  |  |  |
| 9.2.0554 | GTK4: memory leak in free_menu() | `free_menu` |  |  |  |  |  |  |  |  |  |
| 9.2.0571 | Vim9: memory leak in compile_nested_function() on failure | `compile_nested_function` |  |  |  |  |  |  |  |  |  |
| 9.2.0578 | GTK4: :unmenu does not remove entries from the menubar | `gui_mch_destroy_menu, gui_mch_menu_set_tip` |  |  |  |  |  |  |  |  |  |
| 9.2.0661 | unintended wipe of Vim's temp dir, causes errors | `vim_closetempdir, vim_tempname` |  |  |  |  |  |  |  |  |  |
| 9.2.0708 | Leaks in do_autocmd in error case | `do_autocmd` |  |  |  |  |  |  |  |  |  |
| 9.2.0711 | leak in ins_compl_infercase_gettext() in error case | `ins_compl_infercase_gettext` | ✅ | ✅ |  |  |  |  |  |  |  |
| 9.2.0747 | cscope: connection leak when growing the array fails | `cs_insert_filelist` |  |  |  |  |  |  |  |  |  |
| 9.2.0750 | completion: 'autocompletedelay' deferral leaks state | `inchar_loop, ins_compl_arm_autocomplete_delay, ins_compl_autocomplete_pending, v` |  |  |  |  |  |  |  |  |  |
| 9.2.0773 | Memory leak in evalfunc.c on alloc failure | `f_getchangelist, f_getjumplist, get_matches_in_str` |  |  | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0774 | Memory leak in f_getscriptinfo() on alloc failure | `f_getscriptinfo` | ✅ | ✅ |  |  |  |  |  |  |  |
| 9.2.0775 | Memory Leak in highlight_get_info() on alloc failure | `highlight_get_info` |  |  |  |  |  |  |  |  |  |
| 9.2.0776 | Memory leak in sign_getlist() on alloc failure | `sign_getlist` |  |  |  |  |  |  |  |  |  |
| 9.2.0777 | Memory leak in add_defer() on alloc failure | `add_defer` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0778 | Memory Leak in compile_dict() on alloc failure | `compile_dict` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0779 | Memory leak in type_name_func() on alloc failure | `type_name_func` |  |  |  |  |  | ✅ |  |  |  |
| 9.2.0780 | Memory leak in evalvars.c on alloc failure | `ex_let_vars, tuple_append_tv` | ✅ |  |  |  |  |  |  |  |  |
| 9.2.0797 | Memory leak in get_qfline_items() on alloc failure | `get_qfline_items` |  |  |  |  |  |  |  |  |  |
| 9.2.0798 | Memory leak in compile_expr6() on alloc failure | `compile_expr6` |  |  |  |  |  |  |  |  |  |
| 9.2.0799 | Memory leak in compile_def_function_body() on alloc failure | `compile_def_function_body` |  |  | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0800 | Memory leak in call_func() on alloc failure | `call_func` |  |  |  |  |  |  |  |  |  |
| 9.2.0801 | Memory leak in f_getreginfo() on alloc failure | `f_getreginfo` |  |  |  |  |  |  |  |  |  |
| 9.2.0802 | Memory leak with list_append_dict/dict_add_list on alloc failure | `f_cmdcomplete_info, f_diff, f_getmatches, f_hlget, f_term_getcursor, f_term_scra` |  |  | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| 9.2.0803 | Memory leak on alloc failure with taglist/gettagstack() | `get_tags, get_tagstack` |  |  |  |  |  |  |  |  |  |
| 9.2.0816 | GTK4: Memory leak in gui_gtk_set_dnd_targets() | `gui_gtk_set_dnd_targets` |  |  |  |  |  |  |  |  |  |
| 9.2.0854 | memory leak when reading a spell file with SN_SAL and SN_SOFO | `set_sofo, slang_clear, slang_free` |  |  |  |  |  |  |  |  |  |
| 9.2.0902 | Vim9: iterating over a tuple leaks memory | `next_for_item` |  |  |  |  |  |  |  |  |  |
| 9.2.0933 | u_read_undo() leaks the file name when the undo file owner differs | `u_read_undo` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |

## Summary

| run | reported bugs | matching an upstream fix | distinct fixes hit | unmatched (FP or unfixed) |
|---|---|---|---|---|
| codeql | 42 | 27 | 24 | 15 |
| codeql-vanilla | 31 | 15 | 14 | 16 |
| infer-argn | 30 | 22 | 16 | 8 |
| infer-cdb | 37 | 22 | 16 | 15 |
| infer-refA | 32 | 24 | 16 | 8 |
| infer-refE | 34 | 23 | 17 | 11 |
| infer-refF | 29 | 21 | 16 | 8 |
| infer-shim2 | 30 | 22 | 16 | 8 |
| infer-vanilla | 0 | 0 | 0 | 0 |
