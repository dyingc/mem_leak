# Draft upstream PRs for vim/vim

Three leaks found by the MemHint reproduction that are still present on `master`
(a96c3bc1, 2026-09). Patches are `git format-patch` output against that commit; the
branches live in `subjects/vim_master` (a worktree of upstream master):
`memhint/f_setmatches-leak`, `memhint/barline_parse-leak`, `memhint/string_reduce-leak`.

Format follows the PRs the MemHint authors had merged (e.g. vim/vim#19516, #19531):
title `Fix memory leak in \`func()\` in \`src/file.c\``, a **Problem** section quoting the
code, a **Solution** section. No `version.c` change — the maintainer adds the patch number.

Verification: builds of master with and without the patches, driven by the trigger
scripts in `triggers/` — LeakSanitizer for `barline_parse()`, gdb reading `first_list` /
`current_funccal` for the other two (they stay reachable, so LSAN cannot see them).
See `VERIFICATION.md`.

---

## PR 1 — `Fix memory leak in f_setmatches() in src/match.c`

Patch: `f_setmatches.patch`

### Problem

In `f_setmatches()`, when an entry has no `pattern` key (a match created by
`matchaddpos()`), a list is allocated to collect the positions:

```c
if (s == NULL)
{
    s = list_alloc();
    if (s == NULL)
        return;
}

// match from matchaddpos()
for (i = 1; i < 9; i++)
{
    sprintf((char *)buf, (char *)"pos%d", i);
    if ((di = dict_find(d, (char_u *)buf, -1)) != NULL)
    {
        if (di->di_tv.v_type != VAR_LIST)
            return;
```

If a `posN` value is not a List the function returns immediately, and `s` is never
released. It is only handed to `match_add()`/`list_unref()` further down on the
success path. The leak is triggered from Vim script:

```vim
call setmatches([{'group': 'Search', 'pos1': 'notalist', 'priority': 10, 'id': 4}])
```

The list stays on `first_list` until the next garbage collection, the same situation as
the one fixed in patch 9.2.0065 (`invoke_sync_listeners()`).

### Solution

Free the list before returning.

---

## PR 2 — `Fix memory leak in barline_parse() in src/viminfo.c`

Patch: `barline_parse.patch`

### Problem

When a viminfo bar line carries a long string split over continuation lines
(`|{bartype},>{len}` followed by `|<...` lines), `barline_parse()` reassembles it
into an allocated buffer:

```c
buf = alloc(len + 1);
...
p = buf;
```

The string is then unescaped in place. If the closing quote is missing, the loop
bails out:

```c
while (*p != '"')
{
    if (*p == NL || *p == NUL)
        return TRUE;  // syntax error, drop the value
```

This return happens before ownership of `buf` is transferred
(`value->bv_string = s` / `value->bv_tofree = buf`), so `buf` leaks for every
malformed continuation string in a viminfo file, e.g.

```
|1,>5
|<"abc
|<x
```

### Solution

Free `buf` on that early return when the string being parsed is the reassembled
buffer (`s == buf`); in the other case `p` points into `vir_line`, which is not
owned here.

---

## PR 3 — `Fix memory leak in string_reduce() in src/strings.c`

Patch: `string_reduce.patch`

### Problem

`string_reduce()` creates one `funccall_T` for all the `eval_expr_typval()` calls
and removes it at the end:

```c
fc = eval_expr_get_funccal(expr, rettv);

for ( ; *p != NUL; p += len)
{
    ...
    r = eval_expr_typval(expr, TRUE, argv, 2, fc, rettv);

    clear_tv(&argv[0]);
    clear_tv(&argv[1]);
    if (r == FAIL || called_emsg != called_emsg_start)
        return;
}

if (fc != NULL)
    remove_funccal();
```

When the expression fails or gives an error, the function returns without calling
`remove_funccal()`, leaking the funccall and leaving it in `current_funccal`.
`list_reduce()` in the same situation uses `break`, which is the intended pattern.
The funccall is only created for a compiled callable, so the trigger needs a Vim9
lambda that fails at runtime:

```vim
vim9script
silent! echo reduce("abc", (acc, c) => [][0])
```

After this call `current_funccal` still points at the lambda's funccall (see VERIFICATION.md).

### Solution

Use `break` instead of `return`, as `list_reduce()` does, so the funccall is removed.
