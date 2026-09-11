**Superseded (2026-09-08/09): the current patch set is `tip-9.2.1054/` (nine diffs against
5d934b1b, PR texts in `tip-9.2.1054/PR_DESCRIPTIONS.md`, red-team review in
`CHALLENGE_RESPONSE.md`). This file documents the earlier five against a96c3bc1.**

## Status at 2026-09-11

One of the nine is upstream: **`strings.diff` (`string_reduce`) was filed as vim/vim#21255 and
accepted as patch 9.2.1058**, "string_reduce() leaves a stale funccall on error". The other eight
are rebased, reviewed and written up, and are waiting on approval to submit -- they do not need
re-verification.

Re-checked today against upstream master, now at **9.2.1067** (the patch set is still named for
5d934b1b / 9.2.1054, which is stale but left alone so the review trail stays readable):

- All eight remaining diffs `git apply --check` cleanly at 9.2.1067.
- Exactly one upstream commit in 9.2.1054..9.2.1067 touches any of the eight target files:
  `e49b4b5c patch 9.2.1064: Coverity: possible integer underflow in barline_parse()`. That is a
  different defect in the same function as our #2 -- it changes `n > 0` to `n > 2` in the
  NL/CR stripping loop and does nothing about the `buf` leak. Our hunk is elsewhere and still
  applies. Worth knowing that Coverity is currently looking at this function.
- Spot-checked that the leaking code is still present in `match.c`, `json.c` and `if_xcmdsrv.c`.

These eight are **not** part of `notes/vim-defect-verification-request.md`. That file covers ten
*new* candidates found later and never verified; these eight already went through the red-team
review in `CHALLENGE_RESPONSE.md`. The only overlap is a coincidence of file: item 3 there is
`serverSendToVim`, which lives in `src/if_xcmdsrv.c` alongside our #9 `serverRegisterName`, but is
a different function and a different defect.

# Draft upstream PRs for vim/vim

Five leaks found by the MemHint reproduction that are still present on `master`
(a96c3bc1, 2026-09). Patches are `git format-patch` output against that commit; the
branches live in `subjects/vim_master` (a worktree of upstream master):
`memhint/f_setmatches-leak`, `memhint/barline_parse-leak`, `memhint/string_reduce-leak`,
`memhint/json_encode_lsp_msg-leak`, `memhint/parse_generic_func_type_args-leak`.

PRs 1-3 come from the CodeQL run; PRs 4-5 were found only by Infer, after its pattern
syntax was corrected (see `COMPARISON.md`).

Format follows the PRs the MemHint authors had merged (e.g. vim/vim#19516, #19531):
title `Fix memory leak in \`func()\` in \`src/file.c\``, a **Problem** section quoting the
code, a **Solution** section. No `version.c` change — the maintainer adds the patch number.

Verification: builds of master with and without the patches, driven by the trigger
scripts in `triggers/` — LeakSanitizer for `barline_parse()`, `json_encode_lsp_msg()` and
`parse_generic_func_type_args()`, gdb reading `first_list` / `current_funccal` for the
other two (they stay reachable, so LSAN cannot see them). See `VERIFICATION.md`.

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

---

## PR 4 — `Fix memory leak in json_encode_lsp_msg() in src/json.c`

Patch: `json_encode_lsp_msg.patch`

### Problem

`json_encode_lsp_msg()` gives up when the value cannot be encoded:

```c
    ga_init2(&ga, 1, 4000);
    if (json_encode_gap(&ga, val, 0) == FAIL)
	return NULL;
```

On failure `json_encode_gap()` does not leave the growarray empty — it clears what was
encoded so far and puts an allocated empty string in its place:

```c
    if (json_encode_item(gap, val, get_copyID(), options) == FAIL)
    {
	ga_clear(gap);
	gap->ga_data = vim_strsave((char_u *)"");
	return FAIL;
    }
```

That string is never released, because the caller returns without touching `ga`.
`json_encode()` returns `ga.ga_data` to its caller on this path, so only the LSP variant
leaks. Encoding fails for a Funcref, so the leak is reachable from Vim script whenever a
channel is in LSP mode:

```vim
let job = job_start(['cat'], {'in_mode': 'lsp', 'out_mode': 'lsp'})
call ch_sendexpr(job_getchannel(job), {'method': 'test', 'params': function('tr')})
```

### Solution

Clear the growarray before returning.

---

## PR 5 — `Fix memory leak in parse_generic_func_type_args() in src/vim9generics.c`

Patch: `parse_generic_func_type_args.patch`

### Problem

When the type argument is a composite type, `type_name()` builds the name in allocated
memory and hands ownership to the caller through its second argument:

```c
	char	*ret_free = NULL;
	char	*ret_name = type_name(type_arg, &ret_free);

	// create space for the name and the new type
	if (ga_grow(&gfatab->gfat_args, 1) == FAIL)
	{
	    vim_free(ret_free);
	    return NULL;
	}
	...
	generic_arg->gt_name = alloc(STRLEN(ret_name) + 1);
	if (generic_arg->gt_name == NULL)
	    return NULL;
	STRCPY(generic_arg->gt_name, ret_name);
	vim_free(ret_free);
```

The `ga_grow()` failure path frees `ret_free`, but the `alloc()` failure path a few lines
below returns without freeing it. Reaching it needs both an allocation failure and a
composite type argument, e.g. `Identity<list<number>>([1])`.

This is the same pattern as the batch of fixes in patches 9.2.0773-9.2.0803.

### Solution

Free the name before returning, as the neighbouring failure path already does.
