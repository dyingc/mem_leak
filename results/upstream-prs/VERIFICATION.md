# Verification of the five patches (upstream master a96c3bc1, 2026-09-06/08)

Builds of `subjects/vim_master` (a worktree of upstream master), each once with the
patches applied ("fixed") and once without ("orig"):

* ASAN: `CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" LDFLAGS=-fsanitize=address ./configure --with-features=huge --enable-gui=no --with-x=no`
* debug: `CFLAGS="-O0 -g"` (same configure), used for gdb probes.

Why two methods: LeakSanitizer only reports memory that is *unreachable* at exit. The
`barline_parse()` buffer is unreachable, so LSAN sees it. The `f_setmatches()` list stays on
the global `first_list` chain and the `string_reduce()` funccall stays in the global
`current_funccal`, so both are still reachable and invisible to LSAN; for those two we start
Vim normally, let the trigger run, `:sleep`, and attach gdb to read the globals
(`triggers/attach.gdb`).

## 1. barline_parse() — LeakSanitizer

Trigger: `triggers/t_viminfo.vim` reading `triggers/bad.viminfo`
(`|1,>5` continuation string without closing quote).

```
$ ASAN_OPTIONS=detect_leaks=1 ./vim_orig --not-a-term -u NONE -es -S t_viminfo.vim
```
orig (`barline_parse_asan.txt`):
```
Direct leak of 6 byte(s) in 1 object(s) allocated from:
    #0 in malloc
    #1 in lalloc src/alloc.c:246
    #2 in alloc src/alloc.c:151
    #3 in barline_parse src/viminfo.c:1059
    #4 in read_viminfo_barline src/viminfo.c:2812
    #5 in read_viminfo_up_to_marks src/viminfo.c:2888
    #6 in do_viminfo src/viminfo.c:2996
    #7 in read_viminfo src/viminfo.c:3093
    #8 in ex_viminfo src/viminfo.c:3392
SUMMARY: AddressSanitizer: 6 byte(s) leaked in 1 allocation(s).
```
fixed: no leak reported.

## 2. f_setmatches() — gdb attach

Trigger: `triggers/a_setmatches.vim`
(`setmatches([{'group': 'Search', 'pos1': 'notalist', 'priority': 10, 'id': 4}])`, then `:sleep 30`).
`list_alloc()` prepends to `first_list`, so a leaked list is the chain head.

| build | `first_list` head | `current_funccal` |
|---|---|---|
| baseline (no trigger), orig | refcount=1 len=0 | nil |
| **orig** | **refcount=0 len=0** ← orphan list from `f_setmatches()` | nil |
| fixed | refcount=1 len=0 (same as baseline) | nil |

Note: an orphan list on `first_list` is reclaimed by Vim's next `garbage_collect()`,
so the leak is bounded; upstream nevertheless treats this pattern as a bug to fix
(e.g. patch 9.2.0065, `recorded_changes = list_alloc()` in `invoke_sync_listeners()`).

## 3. string_reduce() — gdb attach

Trigger: `triggers/a_reduce9.vim`. The funccall is only created for a *compiled* (Vim9)
callable (`eval_expr_get_funccal()` returns NULL for legacy lambdas), and the leak needs a
runtime error, so the script is:

```vim
vim9script
silent! echo reduce("abc", (acc, c) => [][0])   # E684 at runtime
sleep 30
```

| build | `current_funccal` after `reduce()` returned |
|---|---|
| **orig** | **0x…  funccal.func=\<lambda\>…** ← funccall never removed |
| fixed | nil |

Besides the leaked `funccall_T`, the stale `current_funccal` means later code runs
with a wrong function context until something else pops it.

## Reproducing

```bash
cd subjects/vim_master && git checkout memhint/all      # or a single memhint/<fix>-leak branch
# build vim_fixed / vim_orig as above, then:
cd results/upstream-prs/triggers
ASAN_OPTIONS=detect_leaks=1 ../../../subjects/vim_master/vim_orig --not-a-term -u NONE -es -S t_viminfo.vim
../../../subjects/vim_master/vim_dbg_orig --not-a-term -u NONE -i NONE -es -S a_setmatches.vim </dev/null &>/dev/null & sleep 2
gdb -nx -q -batch -x attach.gdb -p $!
```

## 4. json_encode_lsp_msg() — LeakSanitizer

Binaries `vim_n_orig` / `vim_n_fixed` (ASAN, master with and without the json.c patch).
Trigger: `triggers/t_lsp_json.vim` — a channel in LSP mode, sending a Dict that contains a
Funcref, three times. JSON encoding rejects the Funcref, and `json_encode_gap()` leaves an
allocated empty string behind that the caller drops.

```
$ cd results/upstream-prs/triggers
$ ASAN_OPTIONS=detect_leaks=1 ../../../subjects/vim_master/vim_n_orig --not-a-term -u NONE -i NONE -es -S t_lsp_json.vim
```

orig (`json_lsp_asan.txt`): `Direct leak of 3 byte(s) in 3 object(s)` — one byte per call,
allocated in `vim_strsave` at `json.c:34`, called from `json_encode_gap` and
`json_encode_lsp_msg` at `json.c:104`, reached through `ch_expr_common` / `f_ch_sendexpr`.

fixed: no leak reported.

## 5. parse_generic_func_type_args() — LeakSanitizer with a forced allocation failure

This leak is on an allocation-failure path, so it needs the allocation to fail. Vim's
`test_alloc_fail()` can only target call sites that carry an allocation id, and this one
does not, so the two verification binaries (`vim_g_orig` / `vim_g_fixed`, built by
`subjects/vim_master/build_verify_generic.sh`) add one:

* `alloc.h` gains `aid_generic_name` (value 35) before `aid_last`;
* the call becomes `alloc_id(STRLEN(ret_name) + 1, aid_generic_name)`.

**Neither change is part of the submitted patch** — they exist only to make the failure
reachable from a test.

The type argument must be a composite type: for `<number>` the name is a static string and
`type_name()` sets `tofree` to NULL, so there is nothing to leak. `triggers/t_gen9.vim`
uses `Identity<list<number>>([1])` and `triggers/t_generic_allocfail.vim` arms the failure:

```vim
call test_alloc_fail(35, 0, 1)
source t_gen9.vim
```

orig (`generics_asan.txt`): `Direct leak of 13 byte(s) in 1 object(s)`, allocated in
`type_name` at `vim9type.c:2767` — that is `ret_free`, the composite type name.

fixed: no leak reported.
