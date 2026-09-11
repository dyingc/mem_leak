# PR bodies — against vim/vim master at patch 9.2.1067 (`90fdb790`)

Same shape as vim/vim#21255: `### Problem`, then `### Solution`. Nothing else — no test
rationale, no severity commentary, no notes to the maintainer. Line numbers are 9.2.1067.

---

## PR 1 — `Fix use-after-free when appending a new list fails`

Files: `src/tuple.c`, `src/list.c`, `src/blob.c`, `src/evalfunc.c`, `src/vim9execute.c`
Diff: `list_free.diff`

### Problem

`list_alloc()` links every new list header into the global chain used for garbage collection,
in `list_init()` (`src/list.c`, lines **74–79**):

```c
    // Prepend the list to the list of lists for garbage collection.
    if (first_list != NULL)
	first_list->lv_used_prev = l;
    l->lv_used_prev = NULL;
    l->lv_used_next = first_list;
    first_list = l;
```

The only code that unlinks it again is `list_free_list()` (lines **269–275**), reached through
`list_free()`:

```c
    // Remove the list from the list of lists for garbage collection.
    if (l->lv_used_prev == NULL)
	first_list = l->lv_used_next;
    else
	l->lv_used_prev->lv_used_next = l->lv_used_next;
    if (l->lv_used_next != NULL)
	l->lv_used_next->lv_used_prev = l->lv_used_prev;
```

Seven functions release such a header with a plain `vim_free()` when appending it fails, for
example `tuple2items()` in `src/tuple.c` (line **877**):

```c
	if (list_append_list(rettv->vval.v_list, l) == FAIL)
	{
	    vim_free(l);
	    break;
	}
```

`first_list` is then left pointing at freed memory. The next `list_alloc()` writes through it
immediately, and a later `garbage_collect()` walks the chain into the freed block.

Forcing `listitem_alloc()` to fail once under AddressSanitizer, then allocating another list:

```
ERROR: AddressSanitizer: heap-use-after-free
WRITE of size 8
    #0 list_init  list.c:76
    #1 list_alloc list.c:93
freed by:
    vim_free   alloc.c:623   <- blob2items blob.c:334
previously allocated by:
    list_alloc list.c:91     <- blob2items blob.c:328
```

The other six are `list2items()` and `string2items()` in `src/list.c`, `blob2items()` in
`src/blob.c`, `f_getchangelist()` and `f_getjumplist()` in `src/evalfunc.c`, and
`add_defer_item()` in `src/vim9execute.c`.

Patch 9.2.0808 fixed the same mistake in `add_regionpos_range()`.

### Solution

Use `list_free()` at those seven sites. The append failed, so no reference was taken:
`list_append_list()` increments `lv_refcount` only after a successful `list_append()`, and
`list_insert_tv()` fails before `copy_tv()`. In `add_defer_item()` the list comes from
`list_alloc_with_items()`, whose `listitem_T`s are embedded in the same allocation;
`list_free_item()` checks `lv_with_items` and does not free them separately.

`listitem_alloc()` uses `ALLOC_ONE` without an id, so `test_alloc_fail()` cannot reach this path.
I can add an alloc id to it if you would like a regression test.

---

## PR 2 — `Fix memory leak in f_setmatches() in src/match.c`

File: `src/match.c`
Diff: `match.diff`, test in `match_test.diff`

### Problem

In `f_setmatches()` in `src/match.c`, a list is allocated to collect the positions of a match
created by `matchaddpos()` (line **1135**):

```c
		    s = list_alloc();
		    if (s == NULL)
			return;
```

and its reference count is then incremented once for every `posN` entry appended to it
(line **1150**):

```c
			list_append_tv(s, &di->di_tv);
			s->lv_refcount++;
```

`match_add()` does not keep a reference, and `list_unref(s)` is called only once after the loop.
With two or more positions the count never reaches zero, so the list and the position lists it
references are not released until the next garbage collection:

```vim
call setmatches([{'group': 'Search', 'id': 4, 'priority': 10, 'pos1': [1,1,1], 'pos2': [2,1,1]}])
```

The reference count of a position list can be read directly:

```vim
let p = [1, 1, 1]
call setmatches([#{group: 'Search', id: 4, priority: 10, pos1: p, pos2: [2, 1, 1]}])
call clearmatches()
echo test_refcount(p)   " 2, expected 1
```

When a `posN` value is not a List the function returns without releasing `s` at all
(lines **1146–1147**):

```c
			if (di->di_tv.v_type != VAR_LIST)
			    return;
```

```vim
let p = [1, 1, 1]
call setmatches([#{group: 'Search', id: 4, priority: 10, pos1: p, pos2: {}}])
call clearmatches()
echo test_refcount(p)   " 2, expected 1
```

`test_match.vim` already exercises this path, at `Test_setmatches()`:

```vim
call assert_equal(-1, setmatches([{'group' : 'Search', 'priority' : 10, 'id' : 5, 'pos1' : {}}]))
```

### Solution

Take one reference right after `list_alloc()`, drop the per-item increment, and `list_unref()`
the list on the early return.

---

## PR 3 — `Fix memory leak of 'completeslash' in src/buffer.c`

File: `src/buffer.c`
Diff: `buffer_csl.diff`

### Problem

`buf_copy_options()` in `src/option.c` allocates the buffer-local value of `'completeslash'`
(line **7946**):

```c
#ifdef BACKSLASH_IN_FILENAME
	    buf->b_p_csl = vim_strsave(p_csl);
	    COPY_OPT_SCTX(buf, BV_CSL);
#endif
```

`free_buf_options()` in `src/buffer.c` clears every other buffer-local string option, but not
this one. `grep b_p_csl src/buffer.c` returns nothing. So the previous value is dropped each time
a buffer's options are copied again, and once more when the buffer itself is freed.

The option was added in patch 8.1.1769, whose file list does not include `src/buffer.c`.

Only built when `BACKSLASH_IN_FILENAME` is defined.

### Solution

Clear `b_p_csl` in `free_buf_options()`, next to `b_p_cpt`, where `buf_copy_options()` also
puts it.

I cannot build this path here, but the MS-Windows CI compiles it.
