# Nine leak fixes against upstream 5d934b1b (patch 9.2.1054, 2026-09-08)

Re-verified from scratch against the current tip, not against the analysed tag 9.2.0015.
One earlier claim (`edit.c:ins_tab`) was **withdrawn** here as a false positive.

| patch | verification | trigger |
|---|---|---|
| `match.diff` | **empirical**, live-list counter | plain Vim script, no error needed |
| `viminfo.diff` | **empirical**, LSAN | malformed viminfo file |
| `strings.diff` | **empirical**, funccal-chain probe | `reduce()` on a String with a failing closure |
| `json.diff` | **empirical**, LSAN (at 9.2.0015) | `ch_sendexpr()` on an LSP channel |
| `vim9generics.diff` | **empirical**, failure injection + LSAN | allocation failure |
| `vim9class.diff` | **empirical**, failure injection + LSAN | allocation failure |
| `ex_docmd.diff` | *static only* | `:browse redir >`, cancelled — needs a GUI build |
| `gui_gtk_x11.diff` | *static only* | needs a GTK build |
| `if_xcmdsrv.diff` | *static only* | needs X11 client-server |

`match.diff` fixes two distinct leaks, one of them on the normal path: `f_setmatches()`
increments `lv_refcount` once per appended `posN` list but calls `list_unref()` only once,
so every call carrying two or more positions leaks the list and its contents. Measured with
a counter instrumented into `list_alloc`/`list_free_list`: 1000 calls with two positions
leaked 3000 lists, with three positions 4000. LeakSanitizer does not see this because
`list_init()` links every list into the global `first_list` chain, and `garbagecollect()`
does not reclaim them either.

Regression: `make test_match test_viminfo test_vim9_class test_vim9_generics test_json
test_edit test_functions` with all nine applied — 490 tests, no failures.

`CHALLENGE_REQUEST.md` (one level up) is the adversarial-review brief for these patches.
