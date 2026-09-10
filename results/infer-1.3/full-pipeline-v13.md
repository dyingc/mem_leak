# Full MemHint pipeline on Infer v1.3.0 (patched), Vim 9.2.0015

Run 2026-09-10. Controlled so that **only the Infer version varies**: stage 1 is not re-run, both
sides use `results/vim_9_2_0015/hints.json` (493 summaries, 7 free-arg specs, 0 alloc-arg specs),
the same `anchored-argn` pattern set, and the same stage-3 settings.

| | v1.2.0 patched (`infer-argn-adj`) | v1.3.0 patched (`infer-v13argn`) |
|---|---|---|
| analyze wall | 1469 s at `--jobs 6` | **1267 s at `--jobs 1`** |
| analyze peak RSS | (not measured) | 3.93 GB, 0 compactions |
| raw issues | 395 | **564** |
| leak warnings | 295 | **444** |
| Z3 feasible | 261 (11.5% filtered) | 404 (9.0% filtered) |
| functions to the LLM | 180 | 259 |
| LLM-confirmed functions | 47 | 53 |
| adjacent findings (`source: llm-adjacent`) | 11 in 10 funcs | 27 in 25 funcs |
| bugs.json | 66 (55 analyzer + 11 adjacent) | 96 (69 + 27) |
| upstream-PR procedures in bugs.json | **4/9** | **4/9** |

The single-process v1.3.0 run beat the 6-job v1.2.0 run on wall clock. Not investigated yet;
candidates are the two-year gap in Pulse itself and clang 15 -> 21.

## Findings overlap

Of the 57 functions in the v1.2.0 bugs.json, **56 are also in v1.3.0's**. v1.3.0 is close to a
superset: it adds 22 functions and drops one (`socket_server_send_reply`).

## PR5 is back

`parse_generic_func_type_args` (upstream PR5) reaches bugs.json in both runs, as `LLM_ADJACENT`.
The baseline `infer-argn/bugs.json` had 3/9; it is 4/9 once the second call exists. This is the
case the two-call design was built for: call 1 still rejects the mis-attributed `parse_type@308`,
call 2 names `ret_free` at line 313.

## Caveat 1: the recorded baseline of 24 is a different configuration

`infer-argn/llm_verdicts.json` (24 confirmed) was written 2026-09-08 15:00; the commit that put
callee bodies into the stage-3 prompt (`564307c`) landed 18:00 the same day. So the 24 is a
**no-callee** number and the 47 is a **with-callee** number. `REPORT.md`'s figures are pre-callee.

Verified directly: for `f_getreginfo` both prompt variants are still in `llm_cache`, and

    no callees : verdict=false, 0.99  "the list ... is transferred to the dictionary by dict_add_list"
    callees    : verdict=true,  0.99  "dict_add_list increments the list reference count, but
                                       f_getreginfo never releases the caller's original reference"

## Caveat 2: callee context created a false-positive cluster

Adding callee bodies is a net win but it made the model reason about reference counts, and its
premise about Vim's convention is wrong. 15% of confirmed functions in **both** runs rest on it:

    f_getreginfo  f_undotree  get_tagstack  job_info  qf_getprop_defaults  qf_getprop_items
    sign_get_placed_in_buf   (+ get_buffer_signs in v1.3.0)

All of them claim the function "never releases its own reference" after `dict_add_list`. But
`list_alloc()` is `ALLOC_CLEAR_ONE` and `list_init()` never touches `lv_refcount`, so a fresh list
starts at **0**; `dict_add_list`'s `++list->lv_refcount` makes the dict the sole owner. There is no
own reference to release. All 8 allocation sites were checked: every one is `list_alloc()` or
`list_alloc_id()`.

The irony is that the callee body is what causes the error: the model sees `++list->lv_refcount`
inside `dict_add_list` and infers that someone must decrement. Without the body it assumed
ownership transfer, which is correct.

**Zero adjacent findings use this premise**, in either run -- the cluster is a call-1 problem.

## Spot-checks of the other new confirmations

Real:

- `gui_set_fg_color` / `gui_set_bg_color` -- `hl_set_fg_color_name(vim_strsave(name))`, and the
  callee returns early when `syn_name2id("Normal") <= 0` without freeing, though its parameter is
  documented `// must have been allocated`. Clean leak, and **only visible with the callee body**.
- The `dict_add_list`-failure family (`menuitem_getinfo`, `get_padding_border`, `get_tabpage_info`,
  `yank_do_autocmd`, `get_complete_info`) -- when `dictitem_alloc()` returns NULL, `dict_add_list`
  returns FAIL *before* the increment, orphaning the list at refcount 0. Real, OOM-only.

Plausible but narrow:

- `serverSendToVim` -- the `w == None` exit does not free `loosename` while every other exit does,
  but reaching it with `loosename` set needs the `sscanf` in `LookupName` to fail. Separately, that
  loop does `vim_free(loosename)` without NULLing it, which looks like a use-after-free on the next
  iteration.

## The three functions dropped relative to the recorded 24

`compile_catch`, `get_matches_in_str`, `reserve_local` -- exactly the three that
`results/llm-attribution/noise_floor.py` flags as unstable or unreproducible (`compile_catch` is
FFF: its recorded `true` does not reproduce even under the unchanged prompt). No stable
confirmation was lost.
