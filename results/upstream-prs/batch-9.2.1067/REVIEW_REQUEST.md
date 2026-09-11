# Review request: three patches about to be sent to vim/vim

Please adversarially review the three diffs in this directory before they are filed upstream.
They are written against **vim/vim master at patch 9.2.1067, commit `90fdb790`**. Assume nothing
in `PR_BODIES.md` is true until you have checked it against the source yourself — the claims
there are mine.

Vim source to check against: fetch upstream master, or use `subjects/vim_master` in this repo
(note: that clone is **shallow**, truncated at `a04ab5f0` / 2025-12-02, so `git log -S` and
`git blame` searches reach the truncation boundary rather than the real first commit. Ground
everything in the current source tree, not in history).

All three `git apply --check` cleanly at 9.2.1067; that much is already verified, so do not
spend time on it.

## What I am asking for

For each patch: is it **correct**, is it **minimal**, and does the **PR text** describe it
accurately? A wrong claim in the text is as bad as a wrong line of code here — these go to a
maintainer who will read the text first. The bodies follow vim/vim#21255: a `### Problem` section
quoting the code with line numbers, then a short `### Solution`. Every line number and every code
quote in them should be checked against 9.2.1067.

Be specific about `file:line`. If something cannot be settled from the code, say UNCERTAIN
rather than guessing.

---

## Patch 1 — `list_free.diff`: seven `vim_free` to `list_free`

The claim: `list_alloc()` links the header into `first_list` via `list_init()`, only
`list_free_list()` unlinks it, so `vim_free()` on the header leaves a dangling chain entry.
Upstream fixed the identical mistake in `add_regionpos_range()` in patch 9.2.0808.

Check each of these, and please do not take my word for the set being complete or correct:

1. **Is each of the seven sites actually this bug?** Confirm the list really was allocated by
   `list_alloc()`/`list_alloc_with_items()` and really was not appended anywhere before the
   `vim_free()`. A site where the list *was* already linked into a result would make
   `list_free()` a double-free — that is the failure mode I am most worried about, and it is the
   opposite of the bug patch 9.2.0808 fixed.
2. **Is `list_free()` safe at each site?** My argument is that the append failed, so the list is
   empty and its refcount is still 0. Verify that `list_append_list()` and `list_insert_tv()`
   really increment only on success.
3. **`add_defer_item()` is the odd one.** It uses `list_alloc_with_items()`, whose `listitem_T`s
   are embedded in the same allocation. I claim `list_free()` is still right because
   `list_free_item()` checks `lv_with_items`. Confirm, and check whether `list_free_contents()`
   does anything else unsafe on such a list.
4. **Are there more than seven sites?** I inherited this list from an earlier pass. Search for
   the pattern yourself. Missing sites are not fatal but I would rather file once.
5. **Is `list_free()` reachable there?** It early-returns when `in_free_unref_items` is set. Can
   any of these seven run during garbage collection?
6. Does the claim that "the very next `list_alloc()` writes through the stale pointer at
   `list.c:75`" hold, or does something reset `first_list` in between?

## Patch 2 — `match.diff` + `match_test.diff`: `f_setmatches()` reference counting

This one is older and was reviewed once already; I am including it because the PR text was
rewritten and the test matters.

1. **Is the refcount arithmetic right after the patch?** One reference taken at `list_alloc()`,
   the per-append increment removed, `list_unref()` on the early return. Walk the paths: two
   positions, three positions, a non-List `posN`, and the normal exit. Is the count balanced on
   each?
2. **Does `match_add()` really not keep a reference?** The whole patch rests on that.
3. **The test.** `Test_setmatches_pos_refcount` asserts `test_refcount(p1) == 1` before and after.
   Does it fail on the unpatched source and pass on the patched one? If you cannot build, say
   what the count would be in each case and why.
4. **The text says "not released until the next garbage collection", not "never freed".** Confirm
   that framing is right — an earlier draft claimed the lists were never freed, and that was
   wrong. Getting this backwards in public would be embarrassing.

## Patch 3 — `buffer_csl.diff`: `b_p_csl` in `free_buf_options()`

1. **Is `b_p_csl` genuinely never freed?** Grep the whole tree, not just `buffer.c`.
2. **Is the placement right?** I put it after `b_p_cpt` to mirror `buf_copy_options()`. Is
   `free_buf_options()` ordered that way, or by something else?
3. **Is the `#ifdef BACKSLASH_IN_FILENAME` guard correct and necessary?** The field is declared
   under the same guard in `structs.h`; confirm.
4. **Is `clear_string_option()` the right call**, as opposed to `free_string_option()` or
   `VIM_CLEAR()`? Look at what the neighbouring options use and why.
5. **Can this double-free?** `option.c` also exposes `&curbuf->b_p_csl` through `PV_CSL`, so
   `:set completeslash=` writes it through the generic option machinery. Does that path leave a
   pointer that `free_buf_options()` would free twice, or an `empty_option` sentinel that must
   not be freed?
6. I could not compile this path — `BACKSLASH_IN_FILENAME` is not defined on Linux. I only ran
   `gcc -fsyntax-only` with the macro forced. If you can do better, please do.

---

## Also worth your scepticism

- **PR 1 and PR 3 carry no test.** PR 1's path needs an allocation failure and PR 3's code is
  not built on Linux, so I do not see how to write one for either. The bodies say nothing about
  this -- if the maintainer asks, we answer then. Tell me if you can see a test I have missed;
  that is a better outcome than an explanation for its absence.
- **Is filing three at once a mistake?** They touch unrelated files, but they come from one
  author on the same day.
- Anything in `PR_BODIES.md` that overstates severity, or that a reviewer would read as claiming
  more than the evidence supports.
