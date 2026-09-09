# Nine leak fixes against upstream 5d934b1b (patch 9.2.1054, 2026-09-08)

**Status (2026-09-09): red-team reviewed, PR texts in `PR_DESCRIPTIONS.md`, NOT submitted —
waiting for final approval.** Review: `../CHALLENGE_RESPONSE.md`.

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

  With `qall!` inside, `getout()` runs while the frames the orphaned `fc_ectx` points into
  are still poisoned, so ASAN catches the read and aborts before the leak check. With
  `qall!` outside, the chain unwinds far enough that the `funccall_T` becomes unreachable
  and LSAN reports it as a leak instead. Same bug, two exit paths, two symptoms — which is
  also why one side of this review saw only the leak and the other only the SUAR.

  So the PR *may* cite the stack-use-after-return, but the reproduction command has to put
  `qall!` in the script. Naming `ASAN_OPTIONS=detect_stack_use_after_return=1` is not what
  makes it work (it is the default); the placement of `qall!` is.
- **"garbage collection does not reclaim the `setmatches()` lists".** That was this
  author's error, and the review was right to flag it: `garbagecollect(1)` only sets a flag
  and the collection runs from the main loop, which `-es -S script` never enters. With
  `v:testing = 1` and `test_garbagecollect_now()` the count goes 3003 → 3. The lists are
  pinned until the next GC, not leaked forever.

`CHALLENGE_REQUEST.md` (one level up) is the adversarial-review brief for these patches.
