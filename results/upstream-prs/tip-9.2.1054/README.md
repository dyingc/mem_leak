# Nine leak fixes against upstream 5d934b1b (patch 9.2.1054, 2026-09-08)

**Status (2026-09-09): `strings.diff` accepted upstream as `patch 9.2.1058`
(commit `f874bf9e`, closes [#21255](https://github.com/vim/vim/pull/21255)). The other eight
not submitted.** PR texts in `PR_DESCRIPTIONS.md`, review in `../CHALLENGE_RESPONSE.md`.

A Vim PR is always CLOSED rather than merged, so acceptance is checked in the tree, not on
GitHub: `git log --oneline <base>..origin/master --grep=<function>` should show a numbered
patch authored by you carrying `closes: #<pr>` and two Signed-off-by lines.

yegappan asked for a test on #21255, and chrisbra then added a stronger one on top of ours
(`CheckAsan` in `util/check.vim`, plus a subprocess test that sets
`abort_on_error=1` and asserts `v:shell_error`). `notes/from-signal-to-assertion.md`
has the general idea; what these nine need:

| | when | how |
|---|---|---|
| A | the unpatched binary exits non-zero under ASan | `CheckAsan` + `abort_on_error=1` + `RunVim`, assert `v:shell_error` |
| B | it exits 0 but a script-visible object stays referenced | `test_refcount()` — cheaper, but the fallback: it encodes your model of the defect |
| C | only reachable on allocation failure | no test; that is what all 22 upstream fixes did |

`#1 f_setmatches` is a B: LSan cannot see it (the lists hang off the global `first_list`
chain), but the `posN` lists it keeps referenced are script objects, so `test_refcount()`
asserts it directly. `match_test.diff` is that test — it fails on the unpatched tree with
`Expected 1 but got 2` and passes with the fix.

Re-verified from scratch against the current tip, not against the analysed tag 9.2.0015.
One earlier claim (`edit.c:ins_tab`) was **withdrawn** here as a false positive.

| patch | verification | trigger |
|---|---|---|
| `match.diff` | **empirical**, live-list counter | plain Vim script, no error needed |
| `viminfo.diff` | **empirical**, LSAN (reworked 2026-09-09: covers all four leaking paths, not just the unterminated string) | malformed viminfo file |
| `strings.diff` | **empirical**, LSAN, funccal-chain probe, and ASAN stack-use-after-return | `reduce()` on a String with a failing closure or lambda |
| `json.diff` | **empirical**, LSAN (at 9.2.0015) | `ch_sendexpr()` on an LSP channel |
| `vim9generics.diff` | **empirical**, failure injection + LSAN | allocation failure |
| `vim9class.diff` | **empirical**, failure injection + LSAN | allocation failure |
| `ex_docmd.diff` | **empirical**, LSAN | `:browse redir >` in a +browse build run in a terminal (E338), no dialog needed |
| `gui_gtk_x11.diff` | **empirical**, gvim under Xvfb + failure injection + LSAN | `enc=euc-jp`, ambiguous-width char, `alloc()` failure |
| `if_xcmdsrv.diff` | **empirical**, fabricated `VimRegistry` + LSAN | `--clientserver x11 --servername FOO` with 1000 name collisions |

`match.diff` fixes two distinct leaks, one of them on the normal path: `f_setmatches()`
increments `lv_refcount` once per appended `posN` list but calls `list_unref()` only once,
so every call carrying two or more positions keeps the list and its contents alive until the
next garbage collection. Measured with a counter instrumented into
`list_alloc`/`list_free_list`: 1000 calls with two positions leaked 3000 lists, with three
positions 4000. LeakSanitizer does not see this because `list_init()` links every list into
the global `first_list` chain. (An earlier note here said GC does not reclaim them; that was
wrong — GC never ran in the `-es` measurement. It does reclaim them.)

Regression: with all nine applied, `make test_match test_viminfo test_vim9_class
test_vim9_generics test_json test_edit test_functions test_vim9_builtin test_registers
test_history` — 1035 tests, no failures.

## Independent re-verification, 2026-09-09

Every patch was re-checked in fresh worktrees of `5d934b1b` built from scratch (one
unpatched, one with all nine), each claim run against **both** binaries. `origin/master` was
re-fetched: still `5d934b1b`, zero commits on top, so "unfixed at tip" holds.

| # | patch | unpatched | patched |
|---|---|---|---|
| 1 | `match.diff` | 1/2/3 pos → +0 / +3000 / +7000 live lists; error path +2000 | +0 in every shape, `getmatches()` round-trip unchanged |
| 2 | `viminfo.diff` | 6 of 12 continuation shapes leak | 0 in all 12; real viminfo with a `>404` continuation round-trips identically |
| 3 | `strings.diff` | `stack-use-after-return` in `unwind_def_callstack` (qall in script) or `Direct leak ... string_reduce:1037` (qall via -c); chain depth 0→5 | clean in every variant, depth 0; `list_reduce`/`blob_reduce` unaffected in both |
| 4 | `json.diff` | 1 leak | 0 |
| 5 | `vim9generics.diff` | 1 leak (alloc id 35) | 0 |
| 6 | `vim9class.diff` | 1 leak (alloc id 36) | 0 |
| 7 | `ex_docmd.diff` | 1 leak (GTK build in a terminal) | 0 |
| 8 | `gui_gtk_x11.diff` | 8 leak records, one through `gui_gtk_draw_string` | 7 records, that one gone |
| 9 | `if_xcmdsrv.diff` | 1 leak via `serverRegisterName`; 0 without the collision harness | 0 |

Two claims in the review did not survive and have been struck from the PR texts:

- **`stack-use-after-return` in `unwind_def_callstack()` (#3) — this reviewer's claim that
  it was unreproducible was wrong, and is withdrawn.** It reproduces 5/5 on an independently
  built gcc binary. Two errors on my side: my control program was too weak to exercise
  ASAN's fake stack, which led me to conclude gcc cannot detect this class of error at all
  (it can — plain `-fsanitize=address`, and `detect_stack_use_after_return` is on by default
  in libasan 8); and my trigger put `qall!` in `-c` instead of inside the sourced script.

  That last detail is the whole story, and it is deterministic, not a layout accident:

  | invocation | unpatched | patched |
  |---|---|---|
  | `qall!` **inside** the sourced script (`-S`, `-c source`, `-es`, `-u` alike) | `stack-use-after-return` in `unwind_def_callstack`, 5/5 runs | clean |
  | `qall!` passed **outside** via `-c` | `Direct leak of 2192 byte(s)` from `string_reduce:1037`, 0/5 SUAR | clean |

  The explanation this reviewer first gave for that split — "the sourcing frames are still
  alive, so the slot is still poisoned" — is **also wrong**, and is withdrawn. Poisoning has
  nothing to do with it: the `ectx` slot belongs to a `call_def_function()` that returned
  long before, and is poisoned from that moment. Reachability of a node on a *global* list
  is not changed by C stack unwinding either. The correct mechanism is the reviewee's:
  `do_source_ext()` calls `save_funccal()` on entry (scriptfile.c:1831) and
  `restore_funccal()` on exit (scriptfile.c:2047), and `invoke_all_defer()` walks
  `current_funccal` *and* every `funccal_stack` entry's chain (userfunc.c). While the script
  runs, the stale frame sits on one of those chains and `invoke_all_defer()` reads its dead
  `fc_ectx`; when the script ends, `restore_funccal()` drops it from every chain and it
  becomes a pure leak.

  Verified by an experiment that separates the two accounts, which neither of the reviewee's
  cases (C, D) does — both of those are consistent with either story. Put the failing
  `reduce()` in an **inner** script that returns, then call `qall!` from the outer script,
  whose frames are still very much alive: "poisoned slot" predicts a use-after-return, and
  "`restore_funccal()` dropped it" predicts a leak with no use-after-return. Result: leak,
  no use-after-return. Chain-depth probe agrees: 0 in the outer script, 1 in the inner one
  after the failing call, 0 in the outer script once the source returns. Recorded as case G
  in `../redteam/logs/t3-exit-path-matrix.txt`.

  Consequence worth putting in the PR: the wrong function context lasts only for the rest of
  the script that ran the failing `reduce()`. The reproduction command must put `qall!` in
  the script, and should say why.
- **"garbage collection does not reclaim the `setmatches()` lists".** That was this
  author's error, and the review was right to flag it: `garbagecollect(1)` only sets a flag
  and the collection runs from the main loop, which `-es -S script` never enters. With
  `v:testing = 1` and `test_garbagecollect_now()` the count goes 3003 → 3. The lists are
  pinned until the next GC, not leaked forever.

`CHALLENGE_REQUEST.md` (one level up) is the adversarial-review brief for these patches.
