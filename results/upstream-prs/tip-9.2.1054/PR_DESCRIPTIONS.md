# PR texts for the nine fixes against vim/vim 5d934b1b (patch 9.2.1054)

**Status: drafts, NOT submitted. Waiting for final approval.**

One PR per diff, submitted one at a time. Title format follows the MemHint PRs already
merged upstream (vim/vim#19516, #19531). No `version.c` change; the maintainer assigns the
patch number. Each entry ends with the trigger a reviewer can run; the red-team evidence
behind every claim is in `../CHALLENGE_RESPONSE.md` and `../redteam/`.

Submission order (most important first): 3, 1, 2, 4, 7, 5, 6, 9, 8.

---

## 3. `strings.diff` — Fix stale funccall left by string_reduce() in src/strings.c

```
Problem:  reduce() on a String with a compiled closure or lambda that fails
          (error or exception) returns from string_reduce() without calling
          remove_funccal().  The funccall_T created by
          eval_expr_get_funccal() is leaked and stays on the
          current_funccal chain, so code consulting current_funccal
          afterwards sees a frame belonging to a call that has returned.
Solution: Use break instead of return, like list_reduce() does, so the
          funccall is removed on the error path as well.
```

Trigger (one line):

```vim
vim9script
silent! echo reduce("abc", (acc, c) => [][0])
qall!
```

Unpatched ASAN build: `Direct leak of 2192 byte(s) in 1 object(s) ... eval_expr_get_funccal
... string_reduce strings.c:1037`. Five failing calls leak five funccall_T and leave five
frames on the `current_funccal` chain. `list_reduce()`, `blob_reduce()`, `tuple_reduce()`
and the `filter()`/`map()` paths already use `break` and are not affected. A `def` funcref
(`VAR_FUNC`) does not create a funccall and is not affected either.

The frames left on the chain hold an `fc_ectx` pointing at a `call_def_function()` stack
frame that has returned, so they are dangling as well as leaked. That is a code reading,
**not** an observed fault: it was not possible to make AddressSanitizer report a
stack-use-after-return for it, including on a clang build compiled with
`-fsanitize-address-use-after-return=always` (verified against a control program that does
report one). Do not put a stack-use-after-return claim in the PR body.

Note for the PR body: this is not a security report. It needs the user to run a Vim9
script, which is already arbitrary code execution; it goes through the normal patch flow.

---

## 1. `match.diff` — Fix memory leak in f_setmatches() in src/match.c

```
Problem:  f_setmatches() increments lv_refcount of the position list "s"
          once for every "posN" entry it appends, but match_add() does not
          keep a reference and list_unref() is called only once.  With two
          or more positions the list, and the position lists it holds a
          reference to, are not freed until the next garbage collection.
          When a "posN" value is not a List the function returns without
          releasing "s" at all.
Solution: Take one reference right after list_alloc(), drop the per-item
          increment, and list_unref() the list on the early return.
```

Trigger:

```vim
call setmatches([{'group': 'Search', 'id': 4, 'priority': 10, 'pos1': [1,1,1], 'pos2': [2,1,1]}])
call setmatches([{'group': 'Search', 'id': 4, 'priority': 10, 'pos1': [1,1,1], 'pos2': 'notalist'}])
```

Measured with a live `list_T` counter: 1000 calls with two positions keep 3000 lists alive,
with three positions 4000, with a non-List `pos2` 2000. LeakSanitizer does not see them
because `list_init()` links every list into `first_list`. A garbage collection does
reclaim them, so say "until the next garbage collection", not "never freed". Same class as
patch 9.2.0065 (`recorded_changes` in `invoke_sync_listeners()`).

---

## 2. `viminfo.diff` — Fix memory leak in barline_parse() in src/viminfo.c

```
Problem:  barline_parse() puts a value that was split over continuation
          lines ("|{type},>{len}" followed by "|<" lines) into an allocated
          buffer.  The buffer is only handed over when the value is a
          complete quoted string.  If the closing quote is missing, or the
          continued value is a number, empty or garbage, the function
          returns or falls through without freeing it.
Solution: Free the buffer when the string is not terminated, and treat a
          continued value that does not start with a quote as a syntax
          error, since the writer only ever continues strings.
```

Triggers (each leaks one buffer when read with `:rviminfo`; `-i NONE` must not be used):

```
|1,>5
|<"abc
|<x
```
```
|1,>3
|<123
```
```
|1,>1
|<,
```

LeakSanitizer: `Direct leak ... barline_parse viminfo.c:1059` for all of them on 5d934b1b.
The earlier one-path version of this fix left the last two leaking; this diff covers all
four paths and passes `test_viminfo`.

---

## 4. `json.diff` — Fix memory leak in json_encode_lsp_msg() in src/json.c

```
Problem:  When json_encode_gap() fails it clears the growarray and stores
          an allocated empty string in it.  json_encode_lsp_msg() then
          returns NULL without ga_clear(), leaking that string.
          json_encode() and json_encode_nr_expr() return ga_data to the
          caller on the same path and are not affected.
Solution: Clear the growarray before returning NULL.
```

Trigger:

```vim
let job = job_start(['cat'], {'in_mode': 'lsp', 'out_mode': 'lsp'})
call ch_sendexpr(job_getchannel(job), {'method': 'test', 'params': function('tr')})
```

LeakSanitizer: `Direct leak of 1 byte(s)` per call, `vim_strsave` ← `json_encode_gap` ←
`json_encode_lsp_msg` ← `ch_expr_common`.

---

## 7. `ex_docmd.diff` — Fix memory leak in ex_redir() in src/ex_docmd.c

```
Problem:  ex_redir() expands the file name with expand_env_save() and then,
          with FEAT_BROWSE, calls do_browse().  When that returns NULL the
          function returns without freeing the expanded name.  This
          happens when the dialog is cancelled, and always when a +browse
          Vim runs in a terminal, where do_browse() gives E338.
Solution: Free the file name before returning.
```

Trigger, any `+browse` build (e.g. a GTK build) started in a terminal, no display needed:

```vim
browse redir > /tmp/some_file
```

LeakSanitizer: `Direct leak of 4096 byte(s)` per command, `expand_env_save` ← `ex_redir`.

---

## 5. `vim9generics.diff` — Fix memory leak in parse_generic_func_type_args() in src/vim9generics.c

```
Problem:  parse_generic_func_type_args() gets the type name from
          type_name(), which allocates it for composite types.  When the
          alloc() for gt_name fails the function returns without freeing
          it, while the ga_grow() failure a few lines earlier does.
Solution: Free the allocated type name on that path too.
```

Trigger: a composite type argument, e.g. `Identity<list<number>>([1])`, with the `alloc()`
made to fail (the call site has no alloc id, so this needs a temporary `alloc_id()` and
`test_alloc_fail()`, or a debugger). LeakSanitizer: `Direct leak of 13 byte(s)` from
`type_name_list_or_dict`. Same pattern as patches 9.2.0797-9.2.0803.

---

## 6. `vim9class.diff` — Fix memory leak in ex_class() in src/vim9class.c

```
Problem:  ex_class() allocates the class_T and then the class name.  When
          vim_strnsave() for the name fails it jumps to cleanup, which does
          not free the class_T.
Solution: Free the class_T at that point.  It cannot be freed in cleanup,
          because after set_var_const() has taken the class the failure
          path there already releases it through clear_tv().
```

Trigger: `:class Foo` with the `vim_strnsave()` made to fail. LeakSanitizer: `Direct leak of
264 byte(s) ... ex_class`. With the same injection the fixed build is clean; the
`set_var_const()` failure path (`const Foo = 1` followed by `class Foo`) is clean on both.

---

## 9. `if_xcmdsrv.diff` — Fix memory leak in serverRegisterName() in src/if_xcmdsrv.c

```
Problem:  serverRegisterName() allocates a buffer for the numbered name
          candidates inside the retry loop.  When a later iteration gives
          up (X error, or 1000 names already taken) it returns FAIL without
          freeing the buffer.
Solution: Free the buffer before returning FAIL.
```

Trigger (X11 client-server; in 9.2.10xx the socket method is the default, so pass
`--clientserver x11`): register `FOO` and `FOO1` … `FOO999` in the `VimRegistry` property
on the root window from a live window, then start `vim --clientserver x11 --servername FOO`.
Vim prints "Unable to register a command server name" and LeakSanitizer reports
`Direct leak of 13 byte(s) ... serverRegisterName`. A helper that fabricates the registry is
in `../redteam/triggers/xreg.c`.

---

## 8. `gui_gtk_x11.diff` — Fix memory leak in gui_gtk_draw_string() in src/gui_gtk_x11.c

```
Problem:  With 'encoding' not UTF-8, gui_gtk_draw_string() converts the
          string and may need a larger buffer to insert a space after a
          character that is double-wide in 'encoding' but single-wide in
          UTF-8.  When that alloc() fails the converted buffer is not
          freed.
Solution: Free the converted buffer before returning.
```

Trigger: gvim, `:set encoding=euc-jp`, a line containing a Greek letter (2 cells in euc-jp,
1 cell in UTF-8 with 'ambiwidth' single), and the `alloc(convlen + 2)` made to fail.
LeakSanitizer: `Direct leak of 44 byte(s)` from `string_convert` ← `gui_gtk_draw_string`.
