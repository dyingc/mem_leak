### Problem

In `string_reduce()` in `src/strings.c`, one `funccall_T` is created for all the
per-character calls (line **1037**):

```c
    // Create one funccall_T for all eval_expr_typval() calls.
    fc = eval_expr_get_funccal(expr, rettv);
```

and removed after the loop (lines **1059–1060**):

```c
    if (fc != NULL)
	remove_funccal();
```

The error path inside the loop returns directly, skipping it (lines **1055–1056**):

```c
	if (r == FAIL || called_emsg != called_emsg_start)
	    return;
```

The `funccall_T` is leaked and left on the `current_funccal` chain, with an `fc_ectx`
pointing at a `call_def_function()` frame that has already returned. `list_reduce()` in
`src/list.c` has the same loop and the same condition, but uses `break`.

Sourcing this under AddressSanitizer gives a stack-use-after-return in
`unwind_def_callstack()`, from `invoke_all_defer()` at exit:

```vim
vim9script
silent! echo reduce("abc", (acc, c) => [][0])
qall!
```

### Solution

Use `break` instead of `return`, so the error path reaches the existing `remove_funccal()`.
The fix is included in this commit.
