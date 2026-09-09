# Red-team response: the 9 Vim leak patches against 5d934b1b (patch 9.2.1054)

Reviewed 2026-09-08. **Update 2026-09-09:** the recommendations below have been applied to
`tip-9.2.1054/` (`viminfo.diff` replaced by the complete fix, `vim9class.diff` comment
trimmed, PR texts in `tip-9.2.1054/PR_DESCRIPTIONS.md`); rebuilt and re-verified, see §8.
Nothing has been submitted upstream.

Everything below was reproduced from scratch in fresh worktrees of
upstream at `5d934b1b`, **not** in the author's tree. Evidence, triggers and probes are in
`redteam/` (this directory). Nothing in the author's tree or patches was trusted without a
negative control (unpatched binary must leak, patched binary must not, with the same trigger).

Builds (all `-g -O0 -fsanitize=address`, gcc 14):

| binary | tree | configure | patches |
|---|---|---|---|
| `vim-con-orig` / `vim-con-fixed` | `5d934b1b` | `--with-features=huge --disable-gui --without-x` | none / all 9 |
| `vim-gui-orig` / `vim-gui-fixed` | `5d934b1b` | `--with-features=huge --enable-gui=gtk3 --with-x` (xsmp on, +clientserver, +X11) | none / all 9 |
| `vim-alt-fixed` | `5d934b1b` | as console | all 9, with `viminfo.diff` replaced by `redteam/viminfo-complete.diff` |

All six binaries carry the same probe layer (`redteam/instrument.py`, applied *after* the
patches, touching no line a patch changes): a live `list_T` counter and the `current_funccal`
chain depth exposed as `test_getvalue('rt_live_lists')` / `test_getvalue('rt_funccal_depth')`,
and three allocation ids (35 = `gt_name` alloc in `parse_generic_func_type_args`,
36 = class-name `vim_strnsave` in `ex_class`, 37 = `alloc(convlen + 2)` in
`gui_gtk_draw_string`) so `test_alloc_fail()` can fail exactly one allocation.

Upstream tip: `git fetch` and the GitHub API both report `origin/master` = `5d934b1b`
(committed 2026-09-08T20:45Z). Zero commits on top of it, so "unfixed at tip" is trivially
true for all nine; there is no alternative upstream fix to search for.

## 1. Verdict table

| # | patch | real leak? | unfixed at tip? | patch fixes it? | minimal? | regressions? | verdict |
|---|---|---|---|---|---|---|---|
| 1 | `match.diff` `f_setmatches` | **yes**, both (a) and (b), reproduced with live-list counter | yes | **yes**, all four shapes go to +0 | yes, idiomatic (`list_alloc(); ++lv_refcount` as in evalvars.c:3342, digraph.c:1839) | none (`test_match` 16/16, `test_functions` 133/133) | **send upstream**, but fix the description: the lists are *not* "never freed", the next GC reclaims them (see §3.1) |
| 2 | `viminfo.diff` `barline_parse` | **yes**, LSAN | yes | **partial**: fixes the unterminated-string path only; three sibling paths still leak (see §3.2) | the guard `s == buf` is correct, but the fix is incomplete | none (`test_viminfo` 41/41) | **needs rework** → **done**: `tip-9.2.1054/viminfo.diff` now is `redteam/viminfo-complete.diff`, verified on all shapes (§8) |
| 3 | `strings.diff` `string_reduce` | **yes**, and worse than a leak: stale frame → ASAN **stack-use-after-return** at exit (see §3.3) | yes | **yes**: depth stays 1, ASAN error gone | yes, one word, same as `list_reduce()` | none (`test_functions`, `test_vim9_builtin` pass) | **send upstream** as a normal public PR; describe it as a stale funccall / stack-use-after-return, not just a leak (it is not a security issue: needs running a script) |
| 4 | `json.diff` `json_encode_lsp_msg` | **yes**, LSAN at `5d934b1b` (author only had 9.2.0015) | yes | yes | yes | none (`test_json` 7/7, `test_channel` same result as unpatched) | **send upstream** |
| 5 | `vim9generics.diff` `parse_generic_func_type_args` | **yes**, failure injection + LSAN, negative control passes | yes | yes | yes, mirrors the `ga_grow()` path 10 lines above; same shape as 9.2.0797-0803 | none (`test_vim9_generics` 49/49) | **send upstream** |
| 6 | `vim9class.diff` `ex_class` | **yes**, failure injection + LSAN | yes | yes | works; the 3-line comment is longer than upstream's usual 0 lines for this class of fix | none (`test_vim9_class` 140/140; `set_var_const()` FAIL path clean under ASAN) | **send upstream** (optionally trim the comment) |
| 7 | `ex_docmd.diff` `ex_redir` | **yes** — and it needs **no dialog**: any `+browse` build run in a terminal leaks 4 KB per `:browse redir >` (see §5.1) | yes | yes | yes | none (`test_edit` 90/90, `test_gui` same 2 env failures as unpatched) | **send upstream**; reword the trigger |
| 8 | `gui_gtk_x11.diff` `gui_gtk_draw_string` | **yes**, gvim under Xvfb + injected failure (see §5.2) | yes | yes | yes | none (`test_gui` as above) | **send upstream** (alloc-failure only; low severity) |
| 9 | `if_xcmdsrv.diff` `serverRegisterName` | **yes**, 1000 fabricated name collisions (see §5.3) | yes | yes | yes | none (`test_clientserver` 14/14 with X11 build, both binaries) | **send upstream** (needs `--clientserver x11`; low severity) |

## 2. Evidence per patch

Numbers are from `vim-con-orig` → `vim-con-fixed` unless noted. Trigger files are in
`redteam/triggers/`.

### #1 `f_setmatches` (`t1_setmatches.vim`, `t1_gc.vim`, `t1_gc2.vim`)

Live `list_T` count after 1000 calls of each shape (baseline 4):

| shape | unpatched | patched |
|---|---|---|
| `pos1` only | +0 | +0 |
| `pos1`,`pos2` | +3000 | +0 |
| `pos1`,`pos2`,`pos3` | +4000 | +0 |
| `pos2` is a String (error path b) | +2000 | +0 |
| `pos1` is a String (error path b) | +1000 | +0 |

Matches the author's 3000/4000 exactly: `s` plus the N position lists whose refcount
`list_append_tv()` bumped. `match_add()` copies the positions into `mit_pos_array` and
does not keep `pos_list` (match.c:90-95), so `list_unref(s)` after it is right. After the
patch `getmatches()` still returns the two positions, and `clear_matches` frees them.

### #2 `barline_parse` (`t2_*.vim`, `*.viminfo`)

LSAN, `Direct leak ... barline_parse viminfo.c:1059` in every case on the unpatched binary:

| viminfo content | value after `>len` | unpatched | `viminfo.diff` | `viminfo-complete.diff` |
|---|---|---|---|---|
| `\|1,>5` / `\|<"abc` / `\|<x` | unterminated string | 6 B | clean | clean |
| `\|2,>11` / `\|<"abcdefghij` (author's) | unterminated string | 12 B | clean | clean |
| `\|1,>3` / `\|<123` | **number** | 4 B | **4 B** | clean |
| `\|1,>1` / `\|<,` | **empty (comma)** | 2 B | **2 B** | clean |
| `\|1,>2` / `\|<xy` | **garbage** | 3 B | **3 B** | clean |
| `\|2,1,0,>12` / `\|<"echo hello"` | valid history entry | clean | clean | clean, value parsed |

### #3 `string_reduce` (`t3_reduce.vim`, `t3_min.vim`, `t3_def.vim`)

`current_funccal` depth measured from inside a `def` (baseline 1):

| callable × 5 failing calls | unpatched | patched |
|---|---|---|
| closure (`VAR_PARTIAL`, compiled) throwing | 1 → 6 | 1 |
| plain Vim9 lambda `(acc, c) => [][0]` (E684) | 6 → 11 | 1 |
| `def` funcref, `function(Def)`, `function(Def, [])` | +0 | +0 |
| `list_reduce`, `blob_reduce`, `tuple_reduce` with the same closure | +0 | n/a |
| `filter()`/`map()` on String/List/Dict/Blob/Tuple with a throwing closure | +0 | n/a |

The siblings are clean. Note the author's "a plain lambda does not trigger it" is only true
for `def` funcrefs; a Vim9 lambda is a `VAR_PARTIAL` and does trigger it.

### #4 `json_encode_lsp_msg` (`t4_lsp.vim`)

Unpatched: `Direct leak of 3 byte(s) in 3 object(s)` from `vim_strsave` ← `json_encode_gap`
json.c:34 ← `json_encode_lsp_msg` json.c:104 ← `ch_expr_common` channel.c:5309, one per
`ch_sendexpr()` on the LSP channel. The same script also sends the Funcref over a
`json`-mode channel twice and calls `json_encode()`/`js_encode()` on it; those leak nothing,
confirming that only the LSP variant drops `ga.ga_data`. Patched: clean.

### #5 `parse_generic_func_type_args` (`t5_generic.vim`)

`test_alloc_fail(35, 0, 1)` + `Identity<list<number>>([1])`. Unpatched: `Direct leak of 13
byte(s)` from `type_name_list_or_dict` vim9type.c:2577 ← `type_name` ← 
`parse_generic_func_type_args` vim9generics.c:313 (that is `ret_free`). Patched: clean. The
`<number>` control leaks nothing on either (static name, `ret_free == NULL`).

### #6 `ex_class` (`t6_class.vim`)

`test_alloc_fail(36, 0, 1)` + `:class Foo`. Unpatched: `Direct leak of 264 byte(s)` from
`alloc_clear` ← `ex_class` vim9class.c:2128 (the `class_T`). Patched: clean.

Double-free check, as requested in §3 of the brief: `set_var_const()` is called with
`copy = FALSE`, so `free_tv_arg = !copy` is TRUE and every `goto failed` happens *before* `free_tv_arg = FALSE` at evalvars.c:4381; `failed:` calls
`clear_tv(tv_arg)` → `class_unref(cl)` → refcount 1 → 0 → `class_free()`. So on that path
`cl` is already gone and the patch is right not to touch it in `cleanup:`. The `cleanup:`
block (vim9class.c:2760-2802) never references `cl`. Exercised with `const Bar = 1` followed
by `class Bar` twice: no ASAN report, no leak, on both binaries. `eap->ea_class` is left
pointing at the freed class, but its only reader is `fill_evalarg_from_eap()` (eval.c:134),
which is never reached after `ex_class()` returns. Dangling but dead; not a bug today.

### #7, #8, #9 — see §5.

## 3. Findings the author missed or got wrong

### 3.1 The `setmatches()` lists are reclaimed by garbage collection

The brief says "`garbagecollect(1)` does not reclaim them". That is a measurement artefact:
`garbagecollect()` only sets `want_garbage_collect`, and the collection runs from the main
loop (getchar.c:1911), which a `-es -S script` run never enters. `test_garbagecollect_now()`
is a silent no-op unless `v:testing = 1`. With either done properly:

```
after 1000x 2pos                         live_lists=3004
after test_garbagecollect_now (v:testing=1) live_lists=4
after 1000x 2pos                         live_lists=3004
after garbagecollect() (flag only)       live_lists=3004
after main loop                          live_lists=4
```

The bug is real (wrong refcount, lists and their contents pinned until the next GC) but the
PR text must not say "never freed" — a maintainer will check.

### 3.2 `viminfo.diff` fixes one of four leaking paths

After `buf` is assembled and `p = buf`, the dispatch is `isdigit → number`, `'"' → string`,
`',' → empty`, `else → break`. Only the string branch can ever hand `buf` over
(`bv_string = s` / `bv_tofree = buf`); the other three fall out of the loop with `buf`
still owned by nobody. LSAN confirms all three (table in §2). The writer never emits a
non-string continued value, so treating it as a syntax error is safe:

```c
		*p = NUL;
		p = buf;
		if (*p != '"')
		{
		    // A continued value is always a quoted string; anything
		    // else is a syntax error, drop it.
		    vim_free(buf);
		    return TRUE;
		}
```

plus the author's guarded free on the unterminated-string return. `redteam/viminfo-complete.diff`;
`vim-alt-fixed` is clean on all six inputs and passes `test_viminfo` (41 tests).

The `s == buf` guard itself is correct: `buf` is only handed over at the end of the string
branch, after which parsing continues from `nextp` inside `vir_line`, so a later `s` can never
equal `buf`; and the truncated-file path frees and returns immediately. No path was found
with `s == buf` after hand-over, or with `s != buf` and `buf` owned.

### 3.3 `string_reduce` leaves a dangling frame, not just a leak

`create_funccal()` stores `fc_ectx` pointing at the `ectx` local of `call_def_function()`.
When `string_reduce()` returns without `remove_funccal()`, `current_funccal` keeps pointing at
a frame whose `fc_ectx` is a dead stack address. At exit `getout()` → `invoke_all_defer()`
walks the chain and `unwind_def_callstack()` reads through it:

```
ERROR: AddressSanitizer: stack-use-after-return ... vim9execute.c:7047 in unwind_def_callstack
    #1 invoke_funccall_defer userfunc.c:6773
    #2 invoke_all_defer userfunc.c:6790
    #3 getout main.c:1737
Address ... is located in stack of thread T0 at offset 152 in frame call_def_function
```

One line reproduces it: `vim9script | silent! echo reduce("abc", (acc, c) => [][0]) | qall!`.
Anything that consults `current_funccal` after the failed `reduce()` (closure creation,
`:defer`, `get_current_funccal()` users) runs in the wrong scope until something pops it.
The patch removes the error entirely. The PR should say so; it changes how a maintainer
prioritises it.

### 3.4 `:browse redir` leaks without a dialog

`do_browse()` (filepath.c:2596-2600) returns NULL with E338 whenever `gui.in_use` is
false. So `:browse redir > f` in a `+browse` binary run in a terminal (any distro `vim-gtk3`
package) hits the same `return` and leaks the 4096-byte `expand_env_save()` buffer:

```
Direct leak of 12288 byte(s) in 3 object(s) allocated from:
    #3 expand_env_save_opt misc1.c:1414
    #4 expand_env_save misc1.c:1402
    #5 ex_redir ex_docmd.c:8925
```

The patch fixes both the cancelled-dialog and the console case. The other nine
`do_browse()` callers pass `eap->arg` or a static name, so `ex_redir()` is the only one
that owns its default — no siblings.

### 3.5 Sibling checks that came back clean

- `lv_refcount++` inside an append loop: only match.c:1153. The 30-odd other
  `++lv_refcount` sites are the normal take-a-reference-at-creation pattern.
- `json_encode()`, `json_encode_nr_expr()`: both return `ga.ga_data` (after `ga_append(NUL)`
  reallocs the placeholder `""`) so the caller frees it. Confirmed empirically (§2 #4).
- `list_reduce`, `blob_reduce`, `tuple_reduce`, and every `filter()`/`map()` path that calls
  `eval_expr_get_funccal()`: `break`, not `return`. Confirmed empirically (§2 #3).
- Other `type_name(..., &tofree)` callers (evalfunc.c:265, vim9instr.c:2306,
  userfunc.c:4339/4369/4382): all free `tofree`.
- Other `string_convert(&output_conv, ...)` sites in gui_gtk_x11.c (1688, 5043, 5231, 5541):
  all freed.

## 4. Style and convention

- All nine diffs: tab indentation, no trailing whitespace, no line over 80 columns
  (`redteam/` python scan), `vim_free()` not `free()`, `//` comments, no declarations after
  statements. Nothing a maintainer would bounce on style.
- #6's three-line comment is more than upstream writes for this class (9.2.0797-0803 add the
  `vim_free()` block and nothing else). Harmless; could be cut to one line or dropped.
- #1 uses `list_alloc(); ++s->lv_refcount;` and `list_unref()` at both exits, which is what
  9.2.0065 did for `recorded_changes`. The alternative (leave refcount 0, `list_free()` at
  the error exit) would also work but would make the normal-path `list_unref()` go to -1.
- #3's `break` lands on `if (fc != NULL) remove_funccal();` with `rettv` in exactly the state
  the old `return` left it (`VAR_UNKNOWN` or the last result), so no behaviour change for
  non-error input. `called_emsg` is not touched after the loop.

## 5. Reproduction recipes for #7, #8, #9

All three run here; none is unreachable. Xvfb on `:99` is enough; no window manager needed.

### 5.1 `ex_redir` (#7) — no GUI required at all

```bash
DISPLAY= ASAN_OPTIONS=detect_leaks=1 ./vim-gui-orig --not-a-term -u NONE -N -i NONE -es \
    -S redteam/triggers/t7_browse.vim </dev/null
# unpatched: Direct leak of 12288 byte(s) in 3 object(s) ... ex_redir ex_docmd.c:8925
# patched:   no leak
```

The script runs `:browse redir > file` three times; each gives E338 and leaks once.

### 5.2 `gui_gtk_draw_string` (#8) — Xvfb + gvim + one injected failure

```bash
Xvfb :99 -screen 0 1024x768x24 &
DISPLAY=:99 ASAN_OPTIONS=detect_leaks=1 ./vim-gui-orig -g -f -u NONE -N -i NONE \
    -S redteam/triggers/t8_probe.vim </dev/null 2> t8.log
```

The script sets `encoding=euc-jp` (so `output_conv` is euc-jp → utf-8), shows a Greek
letter (2 cells in euc-jp, 1 cell in UTF-8 with `ambiwidth=single`), arms
`test_alloc_fail(37, 0, 1)` and redraws. `v:errmsg` afterwards is
`E342: Out of memory!  (allocating 4 bytes)` on both binaries, proving the site is hit.
LSAN on the unpatched build (`redteam/logs/t8-gui-orig-lsan.log`):

```
Direct leak of 44 byte(s) in 1 object(s) allocated from:
    #3 iconv_string mbyte.c:5044
    #5 string_convert mbyte.c:5472
    #6 gui_gtk_draw_string gui_gtk_x11.c:6100
    #7 gui_outstr_nowrap gui.c:2668
```

Patched build, same E342, no such block (`redteam/logs/t8-gui-fixed-lsan.log`). GTK/GLib
leak the usual few hundred objects at exit; grep for `string_convert`.

### 5.3 `serverRegisterName` (#9) — 1000 fabricated collisions

`redteam/triggers/xreg.c` writes the `VimRegistry` property on the root window with
`FOO`, `FOO1` … `FOO999`, all owned by a live unmapped window (so `XGetGeometry()` in
`DoRegisterName()` succeeds and every candidate reports "in use", `res == -1`):

```bash
gcc -o xreg redteam/triggers/xreg.c -lX11
DISPLAY=:99 ./xreg FOO 999 &
DISPLAY=:99 ASAN_OPTIONS=detect_leaks=1 ./vim-gui-orig --clientserver x11 --servername FOO \
    --not-a-term -u NONE -N -i NONE -es -c 'qall!' </dev/null
# unpatched: "Unable to register a command server name", v:servername empty, and
#   Direct leak of 13 byte(s) ... serverRegisterName if_xcmdsrv.c:237 ← prepare_server clientserver.c:290
# patched: same message, no leak
# control (no xreg running): v:servername=FOO, no leak
```

Two things the author's static reading did not know: in 9.2.10xx the default client-server
method is the socket server, so the X11 code only runs with `--clientserver x11`; and the
console binary registers at startup (`prepare_server()`), so the GUI is not needed. 13 bytes
is `STRLEN("FOO") + 10`.

## 6. Regression testing

Console build (`vim-con-fixed`, all 9 patches): `make unittests` passed (json, kword,
memfile, message). `make scripttests` was run twice:

1. In the background without a terminal: 28 test files reported "Test caused Vim to exit".
   Every one of them reads typeahead or waits for CursorHold/timers; with stdin at EOF Vim
   exits. Not a patch effect.
2. The 28 files re-run under a pty (`script -qfec "make -k ..." /dev/null`), patched and
   unpatched side by side (`redteam/logs/pty2-rerun-*-test.log`): 26 files, 2274 tests each,
   **identical outcome** — everything passes except `Test_write_backup_symlink`, which fails
   the same way on both binaries (`Expected True but got 0` at its line 16; a backup-file
   `filereadable()` check that does not hold on this tmpfs scratch directory).

`test_channel`: identical result on both binaries (`Test_exit_callback_interval`, marked
flaky, "Vim exited" in the no-tty run; `Test_listen` flaky under the pty).
`test_clientserver`: `Test_client_server_socketserver_custom_path` fails in the scratch tree
because the socket path exceeds the 108-byte `sun_path` limit; from a short-path copy of
`testdir`, with the X11-capable GTK binaries, 14/14 pass on both patched and unpatched.

GTK build (`vim-gui-fixed`) under Xvfb: `test_gui` 53 executed, 2 failed
(`Test_geometry_exact_size`, `Test_tabnew_tabclose_size_stable`: `-geometry` height 15
expected, 13 got — no window manager). Identical failures with `vim-gui-orig`.
`test_gui_init` 3/3, and `test_match test_viminfo test_json test_vim9_class
test_vim9_generics test_functions test_edit` 476/476 on the GTK build.

`vim-alt-fixed` (complete viminfo fix): `test_viminfo` 41/41.

## 7. Author's evidence I could not reproduce, or that is wrong

- "`garbagecollect(1)` does not reclaim them" — wrong, see §3.1.
- "a plain `def` funcref does not trigger #3" — true for `def` funcrefs; but the brief's
  implication that a *closure* is required is false: any Vim9 lambda triggers it.
- "static reading only" for #7, #8, #9 — all three reproduce; #7 does not even need the GUI.
- Everything else (the 3000/4000 list counts, the 12-byte `barline_parse` leak, the 13-byte
  generics leak, the 264-byte class leak, the LSP 3×1-byte leak) reproduced to the byte.

## 8. Post-review update (2026-09-09)

Applied the recommendations to `tip-9.2.1054/`:

- `viminfo.diff` replaced by the complete fix (unterminated string + non-string continued
  value). `vim9class.diff` comment cut to one line. All nine apply cleanly to `5d934b1b`
  (`git apply --check`).
- Rebuilt (`vim-alt2-fixed`, console ASAN, same probe layer) and re-ran the triggers:
  all six viminfo shapes clean, #6 injection clean, #3 minimal trigger clean, #1 counts +0.
- `test_viminfo test_vim9_class test_match test_functions` under a pty: 330/330 passed (41 + 140 + 16 + 133), no failures.
- `tip-9.2.1054/PR_DESCRIPTIONS.md` has the Problem/Solution text and trigger for each PR,
  in the recommended submission order. Not submitted; waiting for approval.
