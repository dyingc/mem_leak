### Problem

`string_reduce()` in `src/strings.c` creates one `funccall_T` up front so that the
per-character `eval_expr_typval()` calls can share it (line **1037**):

```c
    // Create one funccall_T for all eval_expr_typval() calls.
    fc = eval_expr_get_funccal(expr, rettv);
```

and removes it after the loop (lines **1059-1060**):

```c
    if (fc != NULL)
	remove_funccal();
```

But the error path inside the loop returns directly, so `remove_funccal()` is skipped
(lines **1055-1056**):

```c
	if (r == FAIL || called_emsg != called_emsg_start)
	    return;
```

The `funccall_T` is then leaked, and it is still linked into `current_funccal` with an
`fc_ectx` pointing at the stack frame of a `call_def_function()` that has already returned.

`list_reduce()` in `src/list.c` has the same structure and the same condition, and already
uses `break` there, which falls through to the `remove_funccal()` after its loop.
`blob_reduce()`, `tuple_reduce()` and `string_filter_map()` likewise. Only the String
variant of `reduce()` returns.

### Reproduction

```vim
vim9script
silent! echo reduce("abc", (acc, c) => [][0])
qall!
```

With an AddressSanitizer build (`CFLAGS="-g -O0 -fsanitize=address"`,
`ASAN_OPTIONS=detect_stack_use_after_return=1` — the default in libasan 8 but not in older
ones):

```
==2426197==ERROR: AddressSanitizer: stack-use-after-return on address 0x7bd81f10f098
READ of size 4 at 0x7bd81f10f098 thread T0
    #0 in unwind_def_callstack   vim9execute.c:7047
    #1 in invoke_funccall_defer  userfunc.c:6764
    #2 in invoke_all_defer       userfunc.c:6781
    #3 in getout                 main.c:1737
    #4 in ex_quit_all            ex_docmd.c:6302
```

5 runs out of 5, on gcc 14.2, and the same on `-S`, `-c source`, `-es` and `-u`.

The `qall!` has to be **inside** the script. Move it out to `-c 'qall!'` and the same binary
reports a leak instead and no use-after-return:

```
Direct leak of 2192 byte(s) in 1 object(s)
    #3 in eval_expr_get_funccal eval.c:243
    #5 in string_reduce strings.c:1037
```

That is `:source` teardown rather than an artefact, and it bounds the impact:
`do_source_ext()` calls `save_funccal()` on entry (`src/scriptfile.c:1831`), which pushes the
chain onto `funccal_stack` and sets `current_funccal` to NULL, and `restore_funccal()` on
exit (`src/scriptfile.c:2047`). `invoke_all_defer()` walks `current_funccal` *and* every
`funccal_stack` entry's chain. So while the script is still running the stale frame is on
one of those chains and gets read; once the script ends `restore_funccal()` drops it from
every chain and nothing reads it again — it is then only a leak.

In other words the wrong function context lasts for the remainder of the script that ran the
failing `reduce()`. A chain-depth probe gives 0 before the call, 1 after it, and 0 once the
script ends.

A `def` funcref is a `VAR_FUNC`, so `eval_expr_get_funccal()` returns NULL and it is not
affected; a Vim9 lambda or closure is a `VAR_PARTIAL` with a compiled `pt_func` and is.

### Solution

Use `break` instead of `return`, so the error path falls through to the existing
`remove_funccal()` — the same shape `list_reduce()` already has.

### Testing

`make test_functions test_vim9_builtin test_vim9_script` — 793 tests, no failures. With the patch both
symptoms above are gone, on both invocations.
