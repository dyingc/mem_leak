# Verification request: candidate Vim defects found during the MemHint reproduction

All of these came out of the Infer/LLM pipeline on **Vim 9.2.0015**
(`subjects/vim_9_2_0015`), then survived a source-level review in this repo. None has been
reported upstream. They are listed here so a separate session can check them independently
before any becomes a PR.

**What I need from the verification pass, per item:**

1. Confirm or refute the claim by reading the code. Quote `file:line`.
2. Say whether it still exists in **current Vim master**, not only in 9.2.0015.
3. Judge reachability honestly: OOM-only, platform-gated, or normally reachable.
4. Where a test is plausible, say what it would look like. Several of these are OOM-only and
   Vim has no fault-injection harness, so "not testable" is an acceptable answer.
5. Flag anything where my stated reasoning is wrong even if the conclusion happens to hold.

**Two facts to apply throughout — both cost me a wrong conclusion earlier:**

- `list_T` and `dict_T` are **garbage collected**. `list_alloc()` returns refcount 0 and
  `list_init()` (src/list.c:72-80) prepends the list to the global `first_list` chain;
  `list_free_nonref()` / `list_free_items()` (src/list.c:243-296) free every list not marked in
  the current `garbage_collect()` pass, keyed on `lv_copyID`, **not** on `lv_refcount`. So a list
  orphaned at refcount 0 is reclaimed and is *not* a permanent leak. I initially reported several
  such cases as leaks; they are not.
- Plain `char_u *` from `alloc()` / `vim_strsave()` / `vim_strnsave()` is **not** collected.

---

## 1. `getreg_wrap_one_line()` drops its argument on both failure paths

`src/register.c:2713-2732`. OOM-only. Confidence: high.

```c
list_T *list = list_alloc();
if (list != NULL)
{
    if (list_append_string(list, NULL, -1) == FAIL)
    {
        list_free(list);
        return NULL;            /* s leaked */
    }
    list->lv_first->li_tv.vval.v_string = s;
}
return (char_u *)list;          /* list == NULL also lands here; s leaked */
```

The incoming `s` is a `char_u *` (not GC-managed) that the function takes ownership of on the
success path. Neither failure path frees it. `list_append_string(list, NULL, -1)` is called with
`str == NULL`, so its only FAIL exit is `listitem_alloc()` returning NULL (src/list.c:684-687) --
hence OOM-only.

Reached from `get_reg_contents()` (src/register.c:2745-2790) at four allocation sites: 2758
(`get_expr_line_src()`), 2759 (`get_expr_line()`), 2782 (`retval` when `get_spec_reg` set
`allocated`), 2783 (`vim_strsave(retval)`). One fix covers all four.

Callable as `getreg('=', 1)`, `getreg('.', 1)`, `getreg('%', 1)`. Note 2782 is only reachable for
the `'.'` register: `get_reg_contents` passes `errmsg=FALSE`, and every other `allocated==TRUE`
case in `get_spec_reg` (src/register.c:935-1014) is guarded by `if (!errmsg) return FALSE;` or is
intercepted earlier by the `regname == '='` branch at 2753.

## 2. `ex_retab()` leaks `new_vts_array` when `vim_strnsave` fails

`src/indent.c:1738-1752` and `1917-1939`. OOM-only. Confidence: high.

With a non-empty argument (`:retab 4,8`), `tabstop_set()` writes a fresh `ALLOC_MULT(int, ...)`
into `new_vts_array`. If the `vim_strnsave` at 1752 then returns NULL, `new_ts_str` becomes NULL --
and the only code that either installs or frees `new_vts_array` is inside `if (new_ts_str != NULL)`
at 1918-1938. Neither happens, and there is no `return` between 1739 and the end of the function
under `FEAT_VARTABS`, so control reaches the end with the array unowned.

`tabstop_set()` itself is clean: it sets `*array = NULL` for an empty argument and `VIM_CLEAR`s on
its own failure paths. The defect is at the call site.

## 3. `serverSendToVim()` leaks `loosename`, plus a use-after-free next to it

`src/if_xcmdsrv.c:427-432`, with `LookupName` at `src/if_xcmdsrv.c:904-987`.
**Not OOM-only** -- this is the one I consider most worth reporting. Confidence: high on the leak,
medium on the UAF's reachability.

(a) **Leak.** `LookupName(dpy, name, FALSE, &loosename)` allocates `*loose = vim_strsave(p + 1)`
at line 954, inside the branch guarded at 943. `returnValue` is initialised to `None` at 926 and
its only other write is the **unchecked** `sscanf((char *)entry, "%x", &returnValue)` at 953. If
that `sscanf` does not parse, or parses `0` (and `None == 0`), the function returns `None` with
`*loose` already allocated. `serverSendToVim` then takes `if (w == None) { ...; return -1; }` at
427-432, which does not free `loosename`; every other exit does (421, 448-449).

The registry is an `XA_STRING` property on the X root window, so the malformed entry can be
written by any other X client. Each occurrence leaks one server-name string.

(b) **Use-after-free / double-free.** The retry path at 421-422 does `vim_free(loosename); continue;`
without setting `loosename = NULL`. `LookupName` only writes `*loose` inside the 943 branch, so if
the next iteration takes the exact-name match instead (930-937), `loosename` is a dangling pointer
that is then used at 433-434 (`name = loosename`), read at 443/446, and freed again at 448-449.
Reaching it needs the registry to change between iterations, so it is race-dependent -- but the
missing `loosename = NULL` is unconditional.

**Question that decides whether this is a security issue:** can `VimRegistry` be written by
anything other than an X client on the same display? My assessment below says no, and that is what
makes this an ordinary bug rather than a disclosure case. If you find a path where the property is
influenced from outside the display's trust domain, that assessment is wrong -- say so.

## 4. `vim_free()` where `list_free()` is required -- dangling entry left on `first_list`

`src/tuple.c:875-879`, `src/list.c:1145-1149` (`list2items`), `src/list.c:1181-1185`
(`string2items`). OOM-only. Confidence: high on the mechanism, unverified on master.

All three do, on a failure path:

```c
list_T *l = list_alloc();
...
if (list_append_list(rettv->vval.v_list, l) == FAIL)
{
    vim_free(l);            /* should be list_free(l) */
    ...
}
```

`list_alloc()` already linked `l` into the global `first_list` chain via `list_init()`.
`vim_free()` releases the memory **without unlinking**, so `first_list` keeps a dangling pointer
and the next `garbage_collect()` walks into freed memory. This is a use-after-free, not a leak.

`f_getcellwidths` (src/mbyte.c:5782) does the same thing correctly with `list_free(entry)`, which
is what makes the three sites above look like oversights rather than intent.

## 5. `buf_copy_options()` -- `buf->b_p_csl` is never freed anywhere

`src/option.c:7438`; `free_buf_options()` at `src/buffer.c:2428`. **Windows only**
(`#ifdef BACKSLASH_IN_FILENAME`). Confidence: high.

`free_buf_options()` clears roughly sixty buffer-local string options and omits `b_p_csl`.
`src/option.c:7438` (`buf->b_p_csl = vim_strsave(p_csl);`) is the only write to it in the tree, so
every re-copy overwrites the previous pointer and buffer teardown drops it. Unbounded across
buffers, not OOM-gated. On Linux the line is preprocessed away, which is why no analyzer in this
project could have seen it -- it was found by the LLM reading raw source.

## 6. `split_message()` -- degenerate `height` on a short screen

`src/popupmenu.c:1698-1720`. Confidence: medium (mechanism certain, reachability not tested).

`max_height = Rows / 2 - 1` has no lower bound. `height` starts at `2 + ga.ga_len` and is clamped
to `max_height` at 1711-1712. With `Rows` 4 or 5, `height` becomes 1, and then

```c
(*array)->pum_text = vim_strsave((char_u *)"");
(*array + height - 1)->pum_text = vim_strsave((char_u *)"");
```

write to the same slot, dropping the first string. With `Rows <= 3`, `height` is 0 or negative and
the second write goes to index -1, which is worse than a leak. Please check whether `Rows` can
actually be that small while balloon evaluation is active.

**Question that decides whether this is a security issue:** is the out-of-bounds write reachable
through the *message* rather than through `Rows`? `split_message` is also called from
`balloon_split()` (src/evalfunc.c:3688), which a script can call directly, and in some setups the
balloon text comes from an LSP server. My reading is that the overflow depends only on `Rows`, i.e.
on the user's own window size, and that the message cannot drive it. Confirm or refute that.

## 7. `home_replace()` never frees `homedir_env_orig`

`src/filepath.c:2750-2754` and `2830-2831`. **VMS only**. Confidence: high on VMS, dead code
elsewhere.

On VMS `mch_getenv()` is a real function (src/os_vms.c:223-255) whose success paths `alloc()` and
hand ownership to the caller; elsewhere it is `#define mch_getenv(x) (char_u *)getenv(...)`
(src/os_unix.h:452), a borrowed pointer. The only free in `home_replace` is
`if (homedir_env != homedir_env_orig) vim_free(homedir_env);`, which never frees `_orig` on any
path. `home_replace` is called constantly (status line, messages, `expand()`), so on VMS this
leaks per call. Worth confirming whether upstream still supports VMS enough for this to be worth a
patch.

## 8. `gui_set_fg_color()` / `gui_set_bg_color()` leak on an early return in the callee

`src/gui.c` (`gui_set_fg_color`), `hl_set_fg_color_name` in `src/highlight.c`. Confidence: high.

```c
gui_set_fg_color(char_u *name)
{
    gui.norm_pixel = gui_get_color(name);
    hl_set_fg_color_name(vim_strsave(name));
}
```

`hl_set_fg_color_name()`'s parameter is documented `// must have been allocated`, but it returns
early when `name == NULL` or when `syn_name2id((char_u *)"Normal") <= 0`, without freeing. The
freshly `vim_strsave`d string is then lost. Please check when "Normal" can be undefined -- early
startup is my guess, but I have not established it.

## 9. `list_filter_map()` -- `newtv` dropped on the one OOM path

`src/list.c:2652`. OOM-only, and **only a real leak for some payload types**. Confidence: medium.

In the `FILTERMAP_MAPNEW` branch, `if (list_append_tv_move(l_ret, &newtv) == FAIL) break;` is the
only exit in that loop that does not `clear_tv(&newtv)` first. `list_append_tv_move()` fails only
when `listitem_alloc()` returns NULL.

The caveat matters: if `newtv` holds a `list_T`/`dict_T` the garbage collector reclaims it and
there is no leak; if it holds a `char_u *` string, it leaks. Please determine which types can
actually occur here before treating this as a defect.

## 10. `vim_setenv()` -- `envbuf` on the `putenv` branch

`src/misc1.c:2069-2086`. **Not compiled on Linux** (`HAVE_SETENV` is defined,
src/auto/config.h:195). Confidence: low, included for completeness.

On a platform with `putenv` but no `setenv`, `putenv((char *)envbuf)`'s return value is not checked
(src/misc1.c:2084); on failure `envbuf` is unowned. The success path's permanent allocation is
deliberate and documented in the comment there. Separately, on a platform with neither, Vim's own
implementation (src/misc2.c:2674 onwards) *copies* the string, so `envbuf` would leak on the
success path too -- which contradicts that comment. Both are speculative until someone identifies a
platform this still builds on.

---

## Security relevance

Three of the ten are memory-safety bugs rather than plain leaks: **3** (use-after-free and
double-free), **6** (out-of-bounds write), **4** (use-after-free on the next `garbage_collect()`).

My assessment is that **none of them warrants a security disclosure** and all three should go
through ordinary pull requests. The test I applied is whether attacker-controlled input crosses a
trust boundary, not how severe the bug class sounds:

- **3** requires the attacker to already be an X client on the same display. X11 maintains no
  isolation between clients on one display by design -- such a client can already grab the
  keyboard, inject events with XTEST, and read other windows. Being able to corrupt Vim's memory
  adds nothing to that capability. The realistic trigger is a crashed or buggy client leaving a
  malformed registry entry, which makes this a robustness problem. SSH X forwarding and containers
  with a mounted X socket do not change this: a hostile client on such a display already owns the
  session.
- **6** depends on `Rows`, the user's own window size. Not attacker-controlled.
- **4** requires `listitem_alloc()` to fail, i.e. memory exhaustion. Not practically controllable.

For contrast, Vim's actual CVEs are overwhelmingly of the form "open this file" or "source this
script" -- content that does cross a boundary. None of these three is that.

Two findings would overturn this, and each is flagged in its item above: a way to influence
`VimRegistry` from outside the display's trust domain (3), or an out-of-bounds write in
`split_message` driven by the message rather than by `Rows` (6). If either holds, stop and say so
before anything is filed publicly.

Regardless of the verdict, the PR descriptions for 3, 4 and 6 should state plainly that the bug is
a use-after-free or an out-of-bounds write and not merely a leak, and leave it to the maintainers
to escalate if they disagree with the assessment above. That costs nothing and puts the decision
with the people whose call it is.

## Items I checked and concluded are NOT defects

Recording these so nobody re-derives them. Every one of them was reported by the pipeline and
looked plausible:

- `f_getreginfo`, `f_undotree`, `get_tagstack`, `job_info`, `qf_getprop_defaults`,
  `qf_getprop_items`, `sign_get_placed_in_buf`, `get_buffer_signs`, `blob2items`,
  `get_tag_details`, `get_moved_list` -- all rest on "the function never releases its own
  reference after `dict_add_list`". A fresh list starts at refcount 0, so there is no own
  reference; the increment makes the dict sole owner.
- `menuitem_getinfo`, `get_padding_border`, `get_tabpage_info`, `yank_do_autocmd`,
  `get_complete_info` -- the `dict_add_list` FAIL path does orphan the list at refcount 0, but the
  collector reclaims it.
- `buf_set_name` (src/buffer.c:3683-3686) -- the old `b_ffname` is freed on the line above.
- `invoke_sync_listeners`, `get_buffer_info`, `f_getcellwidths`, `tuple2items` (the reported
  allocation), `eval_addlist`, `compile_lambda`, `compile_while`, `option_set_callback_func`,
  `export_myvimdir` -- ownership is transferred, or the cleanup is on a common exit path, or the
  reported path is unreachable. `compile_while` in particular is covered by
  `while (cctx->ctx_scope != NULL) drop_scope(cctx);` in `compile_dfunc_ufunc_cleanup`
  (src/vim9compile.c:4983-4984).
