# Second review pass — what changed and what still needs checking

The first review found three text errors and no code errors. All three are fixed; this file
records what was changed, what I verified myself, and the few things I am still unsure about.

## Verified independently before accepting the first review

I re-read every load-bearing claim against the 9.2.1067 tree rather than taking it on trust. All
of them held:

- `add_defer_item()` really uses `list_insert_tv()` (`vim9execute.c:1105`), not
  `list_append_list()`, and `list_alloc_with_items()` really sets `lv_len = count`
  (`list.c:136`). So the old Solution text was wrong on both halves of that sentence — a
  maintainer opening `vim9execute.c:1099` would have seen it. This was the most valuable catch.
- The `list_init()` quote really spans 74–79, not 72–80 (72 is the signature, 73 the brace), and
  the write is at `list.c:76`, not `:75`.
- `match_test.diff` really said "never released", the phrasing an earlier draft was corrected
  for. It would have lived in the source tree indefinitely.
- `test_match.vim:100` really contains `'pos1' : {}`, so the early-return path is already
  exercised by CI today.
- `empty_option` really is `(char_u *)""` (`globals.h:1694`), `free_string_option()` really does
  not reset the pointer, and `free_buf_options()` really has three call sites
  (`buffer.c:1102`, `option.c:7881`, `option.c:7902`), so it can run twice on one buffer.
  `clear_string_option()` is the only one of the three that is safe.
- `b_p_cpt` sits near the front of `buf_copy_options()` but well down `free_buf_options()`, so
  "the order matches" was overstated. (My own count of "35th of 55" was wrong — it is the 40th of
  60. The regex I used excluded the five entries under `b_s` that come earlier: four `b_s.b_p_*` and one `b_s.b_syn_isk`. The conclusion is
  unaffected, but the number was.)

The ASan trace could not be reproduced here — there is no ASan build on this machine — but its
seven line references all land exactly where this bug would put them (`list.c:76` the UAF write,
`:91` the allocation, `:93` the `list_init()` call, `:273` the unlink, `blob.c:328`/`:334`,
`alloc.c:623`). That is strong corroboration, not verification, and it is recorded as such.

## Changes made

**PR 1**
- Solution rewritten: `list_append_list()` increments only after a successful `list_append()`,
  `list_insert_tv()` fails before `copy_tv()`. The claim that the list is "still empty" is gone —
  it was false for `add_defer_item()`.
- Line reference corrected to 74–79.
- The ASan trace moved into `### Problem`, following 9.2.1058, whose accepted body carried one.
- Added one sentence offering to put an alloc id on `listitem_alloc()` if a regression test is
  wanted. `test_alloc_fail()` cannot reach this path today because `listitem_alloc()` uses
  `ALLOC_ONE` with no id.

**PR 2**
- The test comment now says "not released until the next garbage collection".
- The unreproducible live-counter figure is replaced by a four-line `test_refcount()` snippet the
  maintainer can run.
- A third assertion covers the non-List `posN` early return, and the body points at the existing
  `Test_setmatches()` line that already walks it.
- The test moved to after `Test_matchaddpos_error()`, out from between two screendump tests.

**PR 3**
- "so that the order matches `buf_copy_options()`" → "next to `b_p_cpt`, where
  `buf_copy_options()` also puts it".
- One sentence noting the MS-Windows CI compiles this path.

## What to check this round

1. **The rewritten PR 1 Solution.** It now makes three separate claims —
   `list_append_list()` increments after success, `list_insert_tv()` fails before `copy_tv()`,
   `list_free_item()` skips embedded items. Check each; the previous version was wrong precisely
   because one sentence was stretched to cover a site it did not fit.
2. **The enlarged `match_test.diff`.** Does the third assertion fail on unpatched source and pass
   on patched, like the other two? Does the whole test still pass after relocation — the file
   uses `CheckScreendump` guards nearby, and I moved it across them.
3. **The `test_refcount` snippets in PR 2's body.** Both claim the count reads 2 where 1 is
   expected. Run them.
4. **Whether the ASan trace belongs in the body at all.** It is evidence I did not produce
   myself. If a maintainer asks me to reproduce it and I cannot, that is worse than not showing
   it. Say if you think it should come out.
5. Anything the fixes broke. Three of the four files changed since your pass.
