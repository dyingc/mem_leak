# PR texts — batch against vim/vim master at patch 9.2.1067 (`90fdb790`)

Three independent PRs, in submission order. All three diffs `git apply --check` cleanly at
9.2.1067; `list_free.diff` and `buffer_csl.diff` were generated against that tree, `match.diff`
carries over from the 9.2.1054 set unchanged.

No `version.c` change in any of them — the maintainer assigns the patch number.

---

## PR 1 — `Fix use-after-free when a list append fails in several functions`

Files: `src/tuple.c`, `src/list.c`, `src/blob.c`, `src/evalfunc.c`, `src/vim9execute.c`
Diff: `list_free.diff` (7 lines changed)
Test: none, matching patch 9.2.0808 which fixed the same mistake at another site.

```
Problem:  Seven functions free a freshly allocated list with vim_free() when
          appending it fails.  list_alloc() has already linked the header into
          the global list of lists used for garbage collection, and only
          list_free_list() unlinks it, so first_list is left pointing at freed
          memory.
Solution: Free those lists with list_free(), as add_regionpos_range() does
          since patch 9.2.0808.
```

Body:

> `list_alloc()` calls `list_init()`, which prepends the new header to `first_list`
> (`list.c:72-80`). The only code that unlinks it again is `list_free_list()`
> (`list.c:264-276`), reached through `list_free()`. Releasing the header with a plain
> `vim_free()` therefore leaves a dangling entry in that chain.
>
> The first write through the stale pointer does not have to wait for a garbage collection:
> the very next `list_alloc()` executes `first_list->lv_used_prev = l;` at `list.c:75`.
>
> This is the same mistake patch 9.2.0808 fixed in `add_regionpos_range()`, whose message
> already states that these lists must be freed with `list_free()` "so they are unlinked from
> the garbage-collection chain". The seven sites below were missed then:
>
> | File | Function |
> |------|----------|
> | `src/tuple.c` | `tuple2items()` |
> | `src/list.c` | `list2items()` |
> | `src/list.c` | `string2items()` |
> | `src/blob.c` | `blob2items()` |
> | `src/evalfunc.c` | `f_getchangelist()` |
> | `src/evalfunc.c` | `f_getjumplist()` |
> | `src/vim9execute.c` | `add_defer_item()` |
>
> Calling `list_free()` here is safe: the append failed, so the list is still empty and its
> reference count is still zero — `list_append_list()` increments only after a successful
> append. In `add_defer_item()` the list comes from `list_alloc_with_items()`, whose items are
> embedded in the same allocation; `list_free_item()` checks `lv_with_items` and does not free
> them separately (`list.c`), so `list_free()` is correct there too.
>
> Reached only when `listitem_alloc()` returns NULL, i.e. on allocation failure, which is why
> there is no test — the same reason patch 9.2.0808 carries none.

---

## PR 2 — `Fix memory leak in f_setmatches() in src/match.c`

File: `src/match.c`
Diff: `match.diff`, test in `match_test.diff`

```
Problem:  f_setmatches() increments lv_refcount of the position list "s"
          once for every "posN" entry it appends, but match_add() does not
          keep a reference and list_unref() is called only once.  With two
          or more positions the list, and the position lists it holds a
          reference to, are not released until the next garbage collection.
          When a "posN" value is not a List the function returns without
          releasing "s" at all.
Solution: Take one reference right after list_alloc(), drop the per-item
          increment, and list_unref() the list on the early return.
```

Body:

> ```vim
> call setmatches([{'group': 'Search', 'id': 4, 'priority': 10, 'pos1': [1,1,1], 'pos2': [2,1,1]}])
> call setmatches([{'group': 'Search', 'id': 4, 'priority': 10, 'pos1': [1,1,1], 'pos2': 'notalist'}])
> ```
>
> Measured with a live `list_T` counter: 1000 calls with two positions keep 3000 lists alive,
> with three positions 4000, and with a non-List `pos2` 2000.
>
> Note this is a reference-counting error with a bounded effect, **not** a permanent leak.
> LeakSanitizer does not see it because `list_init()` keeps every list on `first_list`, and a
> garbage collection does reclaim them. The added test asserts the reference count directly
> rather than looking for a leak. Same class as patch 9.2.0065 (`recorded_changes` in
> `invoke_sync_listeners()`).

---

## PR 3 — `Fix memory leak of 'completeslash' in src/buffer.c`

File: `src/buffer.c`
Diff: `buffer_csl.diff`
Test: none — the code is inside `#ifdef BACKSLASH_IN_FILENAME`, so it does not build on the
platforms the test suite runs on here.

```
Problem:  free_buf_options() does not clear 'completeslash'.  buf_copy_options()
          allocates b_p_csl but nothing ever frees it, so the old value is lost
          every time a buffer's options are copied again, and again when the
          buffer is freed.
Solution: Clear b_p_csl in free_buf_options(), next to b_p_cpt so the order
          matches buf_copy_options().
```

Body:

> `free_buf_options()` clears every other buffer-local string option; `b_p_csl` is the only one
> missing. `buf_copy_options()` assigns it with `vim_strsave(p_csl)` under
> `#ifdef BACKSLASH_IN_FILENAME`, so on those builds the previous value is dropped on each
> re-copy and again at buffer teardown. Unlike most leaks of this kind it does not need an
> allocation failure to happen.
>
> The option was added in patch 8.1.1769, which did not touch `src/buffer.c`; the free has been
> missing since then.
>
> Windows-only, so I have not been able to run it — the fix is by inspection, placed to mirror
> the copy order in `buf_copy_options()` (`b_p_cpt`, then `b_p_csl`, then `b_p_cfu`), and
> `b_p_csl` is declared under the same `#ifdef` in `structs.h`.
