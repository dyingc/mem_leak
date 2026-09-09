# Challenge Request: adversarially review 9 proposed Vim memory-leak fixes

You are the **red team**. Someone (an LLM-assisted static-analysis pipeline plus manual
review) claims to have found 9 unfixed memory leaks in Vim and written patches for them.
Your job is **not** to agree. Your job is to break the claims.

Assume every claim below is wrong until you have independently reproduced it. The author
already found one false positive this way (see §5) — there may be more.

## What you are given

- Patches: `results/upstream-prs/tip-9.2.1054/*.diff`, one file per fix, all against
  upstream Vim commit `5d934b1b` ("patch 9.2.1054"), which was `origin/master` on
  2026-09-08.
- The claims table in §2 below.
- Reference repo (contains the analysis pipeline and prior notes):
  `git clone git@github.com:dyingc/mem_leak.git`

## 1. The four questions you must answer for every patch

For each of the 9 patches, answer all four **independently**, with evidence, not reasoning
alone:

1. **Is it a real leak?** Reproduce it on unpatched `5d934b1b`. A code reading is not
   sufficient — the author's `ins_tab` claim looked airtight on paper and was false.
2. **Is it still unfixed at the current upstream tip?** Re-fetch `origin/master`; time has
   passed since `5d934b1b`. Also search the upstream log for a commit that fixed it in a
   different way.
3. **Does the patch actually fix it?** Reproduce with the patch applied and show the leak
   is gone. Also check it fixes the *whole* leak, not one path of several.
4. **Is it the minimal, correct fix, and does it break anything?** See §3.

## 2. The claims

| # | file:function | claim | trigger | how the author verified it |
|---|---|---|---|---|
| 1 | `match.c:f_setmatches` | **two** leaks: (a) `s->lv_refcount++` per appended `posN` but only one `list_unref()`, so with N≥2 positions the list and its N contents are never freed — **normal path, no error needed**; (b) `di_tv` not a list → `return` inside the loop leaks `s` | (a) `call setmatches([{'group':'Search','id':4,'pos1':[1,1,1],'pos2':[2,1,1]}])`  (b) same with `'pos2':'notalist'` | live-`list_T` counter instrumented into `list_alloc`/`list_free_list`, exposed via `test_getvalue()`. 1000 calls: 1 pos → +0, 2 pos → +3000, 3 pos → +4000. LSAN is **blind** here because `list_init()` links every list into the global `first_list` chain. `garbagecollect(1)` does not reclaim them. |
| 2 | `viminfo.c:barline_parse` | `buf = alloc(len+1)` leaks on the `return TRUE; // syntax error` path, which happens before `value->bv_tofree = buf` | malformed viminfo: `\|1,4` / `\|2,>11` / `\|<"abcdefghij` (unterminated quoted string) read at startup | LSAN: `Direct leak of 12 byte(s) ... barline_parse viminfo.c:1059`. **Note:** `-i NONE` suppresses it; must pass `-i <file>` |
| 3 | `strings.c:string_reduce` | `fc = eval_expr_get_funccal()` then `return` on eval error skips `remove_funccal()`, so a stale frame is left on the **global** `current_funccal` chain | `reduce()` over a *String* with a **closure** (`VAR_PARTIAL` whose `pt_func` is compiled) that throws partway | probe counting the `current_funccal` chain depth: 0 → 5 after 5 failing calls. LSAN blind (global chain). A plain `def` funcref is `VAR_FUNC`, not `VAR_PARTIAL`, and does **not** trigger it |
| 4 | `json.c:json_encode_lsp_msg` | on failure `json_encode_gap()` empties the growarray and puts an allocated `""` in it; this function returns NULL without `ga_clear()` | `ch_sendexpr()` on an LSP-mode channel with a Funcref in the message | LSAN, at 9.2.0015 (`results/upstream-prs/json_lsp_asan.txt`) |
| 5 | `vim9generics.c:parse_generic_func_type_args` | `alloc()` for `gt_name` fails → returns without `vim_free(ret_free)`; the neighbouring `ga_grow()` failure path *does* free it | forced allocation failure; needs a **composite** type argument (`type_name()` returns a static string for `<number>`, so `ret_free` is NULL there) | deterministic failure injection + LSAN: `Direct leak of 13 byte(s) ... type_name_list_or_dict` |
| 6 | `vim9class.c:ex_class` | `cl = ALLOC_CLEAR_ONE(class_T)`; if the class-name `vim_strnsave()` fails → `goto cleanup`, and `cleanup:` never frees `cl` | forced allocation failure on `:class Foo` | deterministic failure injection + LSAN: `Direct leak of 264 byte(s) ... ex_class vim9class.c:2128` |
| 7 | `ex_docmd.c:ex_redir` | `fname = expand_env_save()`; under `FEAT_BROWSE` a cancelled dialog returns without `vim_free(fname)` | `:browse redir > file`, cancel the dialog | **static reading only** — needs a GUI build, not reproduced |
| 8 | `gui_gtk_x11.c:gui_gtk_draw_string` | `conv_buf = string_convert()`; `alloc(convlen+2)` failure returns without `vim_free(conv_buf)` | GTK build, `output_conv.vc_type != CONV_NONE`, allocation failure | **static reading only** — needs a GTK build, not reproduced |
| 9 | `if_xcmdsrv.c:serverRegisterName` | `p = alloc()` inside a `do`-while; a later iteration hits `if (res < -1 \|\| i >= 1000) return FAIL` without `vim_free(p)` | X11 client-server, repeated registration failure or 1000 name collisions | **static reading only** — needs X11 + `FEAT_CLIENTSERVER`, not reproduced |

**Claims 7, 8 and 9 have never been executed.** Treat them as the weakest and attack them
first. Build the configurations needed (GTK GUI; X11 with `--enable-xsmp` and client-server)
and either reproduce them or show why the path is unreachable.

## 3. Patch quality — the part that is easy to get wrong

For each patch ask:

- **Double free?** Does any *other* path already free the pointer? Trace every caller and
  every subsequent `goto`/`return`.
  - Specifically for **#6 `ex_class`**: `set_var_const()` is called with `copy = FALSE`,
    which sets `free_tv_arg = TRUE`, and its `failed:` label calls `clear_tv(tv_arg)` →
    `class_unref(cl)` → frees the class. So on the `set_var_const() == FAIL` path `cl` is
    **already freed** and freeing it again at `cleanup:` would be a double free. The patch
    therefore frees `cl` *only* at the name-allocation site, not in `cleanup:`. **Verify
    this reasoning is right**, and check whether `eap->ea_class` (assigned before
    `set_var_const()`) is left dangling on that path — if it is used afterwards, that is a
    separate bug the author did not address.
  - Specifically for **#1 `f_setmatches`**: is `++s->lv_refcount` right after `list_alloc()`
    the idiomatic Vim ownership convention here, or should it be `list_unref()`/`list_free()`
    at the exits with refcount left at 0? Does `match_add()` retain `pos_list`? (Author's
    reading: no — it copies positions into `m->mit_pos_array`.) Is the `s == NULL` reuse
    across loop iterations still correct?
  - Specifically for **#2 `barline_parse`**: the patch frees `buf` only `if (s == buf)`.
    Is that guard right? Find a path where `s != buf` and `buf` still leaks, or where
    `s == buf` but `buf` was already handed to `value->bv_tofree`.
- **Minimal?** Could it be one line instead of five? Does it match how Vim fixes this class
  of bug elsewhere? (Look at the 9.2.0773–0803 series of alloc-failure leak fixes.)
- **Behaviour change?** Does any patch change control flow for non-error inputs?
  #3 changes `return` to `break` — confirm the code after the loop is safe to run in the
  error case (`called_emsg` handling, `rettv` state).
- **Style?** Vim has strict conventions: tabs not spaces, `vim_free()` not `free()`,
  no declarations after statements in old files, comment style. A stylistically wrong patch
  gets rejected regardless of correctness.
- **Related code the author missed.** For each confirmed bug, look for the *same mistake in
  sibling functions*. Examples worth checking:
  - `f_setmatches` refcount pattern — does the same `lv_refcount++`-per-append idiom appear
    anywhere else in the tree?
  - `json_encode_lsp_msg` vs `json_encode` — the author claims only the LSP variant leaks
    because `json_encode` returns `ga.ga_data` on the same path. Confirm.
  - `string_reduce` vs `list_reduce` / `blob_reduce` (`list.c`, `blob.c` also call
    `eval_expr_get_funccal`) — do they have the same missing `remove_funccal()`?
  - `parse_generic_func_type_args` — other `type_name()` callers that ignore `ret_free`.

## 4. How to reproduce (the author's harness)

```bash
git clone https://github.com/vim/vim.git && cd vim && git checkout 5d934b1b
cd src && ./configure --with-features=huge --disable-gui --without-x \
    CFLAGS="-g -O0 -fsanitize=address -fno-omit-frame-pointer" LDFLAGS="-fsanitize=address"
make -j8
ASAN_OPTIONS=detect_leaks=1 ./vim -u NONE -N -i NONE -e -s -S test.vim
```

Two traps the author hit, which you will hit too:

- **LSAN cannot see Vim's `list_T` or `funccall_T` leaks.** Both are linked into global
  chains (`first_list`, `current_funccal`), so LeakSanitizer classes them as reachable and
  reports nothing. For those you must count live objects yourself — instrument
  `list_alloc`/`list_free_list` (or walk the `fc_caller` chain) and expose the count through
  a `test_getvalue()` key.
- **Vim's test Makefile appends a suffix to `$ASAN_OPTIONS`**, so a bare
  `ASAN_OPTIONS=detect_leaks=0` becomes the invalid `detect_leaks=0_test_foo`. Use
  `ASAN_OPTIONS=detect_leaks=0:log_path=/tmp/asanlog` so the suffix lands on a string option.

For allocation-failure paths (#5, #6, and originally #7/#8) the author used a temporary
harness: a `memhint_fail_site(n)` function reading `$MEMHINT_FAIL`, wired into the specific
allocation, so exactly one allocation returns NULL deterministically. Vim's own
`test_alloc_fail()` only works at call sites that already use `alloc_id()` with a dedicated
`aid_*`, which none of these do.

**Always run the negative control**: revert the fix, keep the injection, and confirm the
leak *appears*. If it does not, the claim is dead. That is exactly how the author's
`ins_tab` claim died.

## 5. The known false positive — calibrate against this

`edit.c:ins_tab` was claimed as a leak: `saved_line = vim_strnsave(...)` at line 5233, and
`newp = alloc(...)` failing at 5331 returns `FALSE` before `vim_free(saved_line)` at 5384.

It is **not** a leak. The `newp` block is inside `if (!(State & VREPLACE_FLAG))`, while
`saved_line` is only assigned inside `if (State & VREPLACE_FLAG)`. The two are mutually
exclusive, so on that `return FALSE` the pointer is always NULL. Static reasoning about
"allocated here, returned there, freed later" missed the guard three levels up.

If any of the 9 remaining claims has this shape, find it.

## 6. Regression evidence to reproduce and extend

The author ran, with all 9 patches applied on `5d934b1b`:

```
cd src/testdir && ASAN_OPTIONS=detect_leaks=0:log_path=/tmp/asanlog \
  make test_match test_viminfo test_vim9_class test_vim9_generics test_json test_edit test_functions
```

490 tests executed, all `.res` files empty (Vim writes failures into `.res`). Extend this:
run the **full** suite (`make test`), and add the GUI/X11 configurations needed for #7–#9.

## 7. What to report back

A table with one row per patch:

| # | real leak? | unfixed at tip? | patch fixes it? | minimal? | regressions? | verdict |

`verdict` ∈ {**send upstream**, **needs rework** (say what), **reject** (say why)}.

Then, separately:

- any *additional* leak you found while looking (especially the sibling-function checks in §3);
- any patch that is correct but would be rejected upstream on style or convention grounds;
- your reproduction recipe for #7, #8, #9 (or evidence that they are unreachable);
- anything in the author's evidence you could not reproduce.

Be specific and adversarial. "Looks fine" is not an answer.
