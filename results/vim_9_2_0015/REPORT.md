# MemHint reproduction report — Vim

## Stage 1: summary generation (paper Table IV)

| | #Extr. | #Cand. | #Summ. | #Valid. |
|---|---|---|---|---|
| ours | 11956 | 9318 (22.1% ↓) | 1270 (86.4% ↓) | 493 (61.2% ↓) |
| paper | 11071 | 8539 (22.9% ↓) | 2539 (70.3% ↓) | 688 (72.9% ↓) |

LLM (gpt-5.6-luna) summary generation: 466 calls, 6,052,055 prompt + 409,107 completion tokens, **$1.70** (paper, Gemini 3 Flash: $10.20)

## Stages 2-3: warnings → Z3 → LLM → bugs (paper Table V)

| analyzer | | #Warn. | #Z3 | #LLM-valid. | #bugs reported |
|---|---|---|---|---|---|
| codeql | ours | 455 | 361 (20.7% ↓) | 37 | 42 |
| codeql-vanilla | ours | 268 | 233 (13.1% ↓) | 28 | 31 |
| codeql | paper | 1011 | 86 (91.5% ↓) | 24 | 18 confirmed |
| infer | ours | 102 | 50 (51.0% ↓) | 2 | 2 |
| infer-vanilla | ours | 21 | 21 (0.0% ↓) | 0 | 0 |
| infer | paper | 1032 | 147 (85.8% ↓) | 25 | 15 confirmed |

Paper baselines (Table II): vanilla CodeQL 10, vanilla Infer 3; MemHint 22 unique bugs.

## Validation against upstream (see ground_truth.md)

Upstream Vim commits after v9.2.0015 whose subject mentions *leak* are the oracle; a reported bug matches when (file, function) equals a changed function of such a commit. Unmatched bugs were reviewed manually (manual_review.json).

| run | reported | matches upstream fix | distinct fixes hit | manual TP (unfixed upstream) | manual TP (fixed by a non-'leak' commit) | manual FP | not reviewed |
|---|---|---|---|---|---|---|---|
| codeql | 42 | 27 | 24 | 5 | 3 | 5 | 0 |
| codeql-vanilla | 31 | 15 | 14 | 5 | 4 | 5 | 0 |
| infer | 2 | 2 | 2 | 0 | 0 | 0 | 0 |
| infer-vanilla | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Reported bugs — codeql (42)

| file | function | line | rule | LLM reason | validation |
|---|---|---|---|---|---|
| src/change.c | `invoke_sync_listeners` | 552 | memory-may-not-be-freed | The list allocation is leaked when dict_alloc() fails because the function returns without releasing recorded_changes. | fixed upstream: 9.2.0065 |
| src/clientserver.c | `build_drop_cmd` | 703 | memory-may-not-be-freed | cdp is not freed when a later filename allocation fails and the function returns early. | fixed upstream: 9.2.0066 |
| src/clipboard.c | `clip_wl_init_buffer_store` | 2410 | memory-may-not-be-freed | The allocated store is not freed when ftruncate fails and the function returns NULL. | manual TP-FIXED: In 9.2.0015: store = alloc(); ftruncate() failure returned NULL without vim_free(store). The wl_shm buffer-store code (mch_create_anon_file/ftruncate/wl_shm_create_pool) no longer exists at upstream HEAD, so the leak is gone with it. |
| src/clipboard.c | `clip_wl_receive_data` | 2996 | memory-may-not-be-freed | The converted buffer returned by string_convert is assigned to final but is never freed after clip_yank_selection copies or consumes the data. | manual TP-FIXED: In 9.2.0015: tmp = string_convert(); final = tmp; clip_yank_selection() copies; tmp never freed. At upstream HEAD the conversion moved into clip_convert_data(..., &tofree) followed by vim_free(tofree), so the leak is gone (refactor commit, subject without 'leak'). |
| src/cmdexpand.c | `ExpandFromContext` | 3507 | memory-may-not-be-freed | The allocated buffer is leaked when regular-expression compilation fails and returns early. | fixed upstream: 9.2.0055 |
| src/cmdexpand.c | `expand_pattern_in_buf` | 5013 | memory-may-not-be-freed | If ga_grow fails, the newly allocated match is not added to ga and is not freed before cleanup returns. | fixed upstream: 9.2.0476 |
| src/dict.c | `eval_dict` | 960 | memory-may-not-be-freed | The dictionary allocation leaks on the direct missing-bracket failure return, while allocated items are either inserted or freed. | fixed upstream: 9.2.0079 |
| src/dict.c | `dict_extend_func` | 1340 | memory-may-not-be-freed | When is_new is true, a type-check failure returns without releasing the copied dictionary. | fixed upstream: 9.2.0067 |
| src/eval.c | `eval8` | 4787 | memory-never-freed | The parsed type list is not cleared when evaluation is disabled, while the actual type is cleaned up through that list. | fixed upstream: 9.2.0244 |
| src/eval.c | `eval8` | 4787 | memory-may-not-be-freed | The parsed type list is not cleared when evaluation is disabled, while the actual type is cleaned up through that list. | fixed upstream: 9.2.0244 |
| src/evalvars.c | `heredoc_get` | 871 | memory-may-not-be-freed | The compile failure path returns without freeing the list allocated at line 871. | fixed upstream: 9.2.0105 |
| src/evalvars.c | `ex_let_vars` | 1359 | memory-may-not-be-freed | The newly allocated tuple is not freed when appending an element fails and the function returns. | fixed upstream: 9.2.0780 |
| src/ex_cmds.c | `ex_substitute` | 4105 | memory-may-not-be-freed | The initial substitution buffer is leaked if saving it as old_sub fails, while the concatenated preview line is ownership-transferred to ml_replace. | fixed upstream: 9.2.0056 |
| src/ex_docmd.c | `expand_findfunc` | 7076 | memory-may-not-be-freed | The list returned by call_findfunc is not freed when it is empty or when the files allocation fails. | fixed upstream: 9.2.0106 |
| src/ex_docmd.c | `ex_redir` | 8854 | memory-may-not-be-freed | The original filename allocation is leaked when the browse dialog is cancelled and the function returns before freeing it. | manual TP: fname = expand_env_save(); under FEAT_BROWSE a cancelled dialog returns without vim_free(fname). Unfixed upstream. |
| src/gui_gtk_x11.c | `gui_mch_init` | 3745 | memory-never-freed | Each allocated color object is stored globally but has no visible deallocation or ownership-transfer cleanup. | manual FP: gui.fgcolor/bgcolor/spcolor are process-lifetime globals. |
| src/gui_gtk_x11.c | `gui_mch_init` | 3746 | memory-never-freed | Each allocated color object is stored globally but has no visible deallocation or ownership-transfer cleanup. | manual FP: gui.fgcolor/bgcolor/spcolor are process-lifetime globals. |
| src/gui_gtk_x11.c | `gui_mch_init` | 3747 | memory-never-freed | Each allocated color object is stored globally but has no visible deallocation or ownership-transfer cleanup. | manual FP: gui.fgcolor/bgcolor/spcolor are process-lifetime globals. |
| src/gui_gtk_x11.c | `gui_gtk_draw_string` | 5935 | memory-may-not-be-freed | The converted buffer is not freed when the subsequent replacement allocation fails and the function returns early. | manual TP: conv_buf = string_convert(); alloc(convlen+2) failure returns without vim_free(conv_buf). Same class as patches 9.2.0773-0803 (alloc-failure leaks). Unfixed upstream. |
| src/indent.c | `change_indent` | 1336 | memory-may-not-be-freed | If the second line copy fails, the previously allocated original line is returned without being freed or transferred. | fixed upstream: 9.2.0243 |
| src/insexpand.c | `ins_compl_infercase_gettext` | 633 | memory-may-not-be-freed | wca is not freed when ga_grow fails after the growarray has already been allocated. | fixed upstream: 9.2.0711 |
| src/json.c | `json_decode_item` | 1127 | memory-may-not-be-freed | The allocated dictionary item can remain in an unattached nested dictionary when parsing later fails, and cleanup does not release that dictionary. | manual FP: dictitem freed on dict_add failure, owned by the dict on success. |
| src/list.c | `list_extend_func` | 2968 | memory-may-not-be-freed | The copied list is neither freed nor transferred to the return value on subsequent error exits. | fixed upstream: 7ed37dc5 |
| src/mark.c | `add_mark` | 1475 | memory-may-not-be-freed | If adding the mark string fails, the allocated position list is neither freed nor transferred to the dictionary. | fixed upstream: 9.2.0258 |
| src/match.c | `f_setmatches` | 1128 | memory-may-not-be-freed | The allocated list leaks when a positional entry has a non-list value and the function returns before releasing it. | manual TP: s = list_alloc(); a posN entry that is not a list hits `return;` inside the loop without list_free(s). User-triggerable via setmatches(). |
| src/netbeans.c | `netbeans_file_activated` | 2605 | memory-may-not-be-freed | If bp is NULL after nb_quote succeeds, the function returns without freeing q. | fixed upstream: 9.2.0133 |
| src/optionstr.c | `did_set_pumborder` | 3698 | memory-may-not-be-freed | Invalid custom border tokens jump to error without freeing the allocated token. | manual TP-FIXED: In 9.2.0015: token = vim_strnsave(); the custom: branch has three `goto error` without vim_free(token) (user-triggerable via :set pumborder=custom:bad). Fixed upstream as a side effect of the 9.2.0318 refactor (logic moved to parse_pumopt_border(), which frees token on every failure path); the commit subject does not mention 'leak', so the ground-truth matcher missed it. |
| src/os_unix.c | `socket_server_send_reply` | 9817 | memory-may-not-be-freed | The encoded buffer is not freed when socket_server_write fails. | fixed upstream: 9.2.0134 |
| src/scriptfile.c | `f_getscriptinfo` | 2302 | memory-may-not-be-freed | The allocated pattern and compiled regular expression are not freed when later dictionary or list operations return early. | fixed upstream: 9.2.0774 |
| src/strings.c | `string_reduce` | 1039 | memory-never-freed | The function returns on loop errors before removing the funccall created at the reported allocation site. | manual TP: fc = eval_expr_get_funccal(); `return` on eval error skips remove_funccal(). Still present at upstream HEAD (9.2.0960 fixed only a double-free here). |
| src/tuple.c | `eval_tuple` | 512 | memory-may-not-be-freed | The first-item append failure returns without freeing the allocated tuple, while other exits free or transfer ownership. | fixed upstream: 9.2.0135 |
| src/undo.c | `u_read_undo` | 1861 | memory-may-not-be-freed | The allocated undo filename leaks on the UNIX owner-mismatch early return before the common cleanup path. | fixed upstream: 9.2.0933 |
| src/userfunc.c | `add_defer` | 6654 | memory-may-not-be-freed | saved_name is leaked when ga_grow fails before ownership is transferred to a defer record. | fixed upstream: 9.2.0777 |
| src/vim9class.c | `add_interface_from_super_class` | 916 | memory-may-not-be-freed | The allocated name is leaked when the first ga_grow call fails before ownership is transferred to impl_gap. | fixed upstream: 9.2.0136 |
| src/vim9cmds.c | `compile_for` | 992 | memory-never-freed | The nesting-depth error path returns after creating a scope without dropping it, while the parsed type is owned by the compilation context. | manual FP: scope is linked into cctx->ctx_scope; compile_def_function() drops all scopes on failure. |
| src/vim9cmds.c | `compile_catch` | 1788 | memory-may-not-be-freed | The pattern allocation can leak when generate_PUSHS fails before ownership is transferred, while new_scope failure does not itself leak memory. | manual FP: generate_PUSHS() frees *str on failure. |
| src/vim9compile.c | `compile_assign_index` | 2466 | memory-may-not-be-freed | If instruction generation fails, the allocated key remains locally owned and the function returns without freeing it. | manual FP: generate_PUSHS() frees *str on failure. |
| src/vim9execute.c | `exe_newdict` | 279 | memory-never-freed | If dict_add fails, the newly allocated item is not inserted and is not freed before dict_unref, causing a leak. | fixed upstream: 9.2.0057 |
| src/vim9execute.c | `exe_newdict` | 279 | memory-may-not-be-freed | If dict_add fails, the newly allocated item is not inserted and is not freed before dict_unref, causing a leak. | fixed upstream: 9.2.0057 |
| src/vim9expr.c | `compile_dict` | 1888 | memory-may-not-be-freed | Direct error returns bypass dict_unref, leaking the dictionary and inserted dict items; the literal key is transferred to the generated instruction. | fixed upstream: 9.2.0778 |
| src/vim9expr.c | `compile_dict` | 1966 | memory-may-not-be-freed | Direct error returns bypass dict_unref, leaking the dictionary and inserted dict items; the literal key is transferred to the generated instruction. | fixed upstream: 9.2.0778 |
| src/viminfo.c | `barline_parse` | 1058 | memory-may-not-be-freed | The allocated buffer leaks when parsing the reconstructed string exits early on malformed or unterminated input before ownership is transferred. | manual TP: buf = alloc(len+1); `return TRUE; // syntax error` happens before value->bv_tofree = buf, leaking buf on a malformed viminfo file. Still present at upstream HEAD. |

## Reported bugs — infer (2)

| file | function | line | rule | LLM reason | validation |
|---|---|---|---|---|---|
| src/eval.c | `eval8` | 4841 | MEMORY_LEAK_C | When parse_type() fails during non-evaluating parsing, allocated entries can remain in type_list because cleanup is skipped when want_type is NULL. | fixed upstream: 9.2.0244 |
| src/insexpand.c | `ins_compl_infercase_gettext` | 702 | MEMORY_LEAK_C | The grow failure at line 702 returns after clearing the gap without freeing wca, while the other paths free it. | fixed upstream: 9.2.0711 |

