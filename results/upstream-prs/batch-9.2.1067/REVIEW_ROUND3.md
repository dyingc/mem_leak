# Third pass — one real error fixed, and the ASan evidence is now ours

The second review found one error that would have shipped, two improvements, and a wrong number
in my own notes. Everything below was re-checked against the 9.2.1067 tree before being accepted.

## The error that would have gone out

PR 2's body said the early-return path is already exercised "at `Test_setmatches()`" in
`test_match.vim`. **There is no `Test_setmatches()` in that file.** The line in question,
`test_match.vim:100`, lives in `Test_match()`, declared at line 6 as `function Test_match()` —
with `function`, not `func`. My `grep '^func Test_'` skipped it and I attributed the line to the
nearest plausible name. A `Test_setmatches()` does exist, in `test_expr.vim:743`, and does not
walk this path.

A maintainer would have run `grep Test_setmatches src/testdir/test_match.vim`, found nothing, and
been right to distrust the rest of the body. Fixed to `Test_match()`.

## Also fixed

- PR 1: "Forcing `listitem_alloc()` to fail once" → "...once **with a debugger**". The Solution
  says two paragraphs later that `test_alloc_fail()` cannot reach this path, so without this the
  obvious question is how it was forced at all.
- PR 3: "I cannot build this path here, but the MS-Windows CI compiles it" was **false as of this
  round** — `repro/mingw_csl.sh` cross-compiles it with mingw-w64 and exercises it under Wine.
  Replaced with what was actually run: `:set completeslash=`, 50 buffer create/wipe cycles, and
  400 `buf_copy_options()` re-entries with `'cpo'` containing `S`.
- The header of `PR_BODIES.md` claimed "no notes to the maintainer", which two of the three
  bodies now contradict. Reworded rather than deleting the sentences: offering an alloc id for a
  test, and saying what was run on Windows, are concrete offers, not explanations for an absence.

## My own miscount, corrected

`REVIEW_ROUND2.md` said `b_p_cpt` is the 35th of 55 `clear_string_option()` calls in
`free_buf_options()`. It is the **40th of 60**. My regex matched `&buf->b_p_` and so skipped the
five `&buf->b_s.b_p_*` entries, which come earlier. The conclusion — that `free_buf_options()` is
not ordered like `buf_copy_options()` — is unaffected, but the number was wrong and is in a file
we keep.

## The ASan trace is now first-hand

The previous round flagged that the trace was evidence I had not produced, and that showing
something I could not reproduce would be worse than showing nothing. `repro/asan_uaf.sh` settles
it: it builds 9.2.1067 in a throwaway worktree with `-fsanitize=address`, uses gdb to make
`listitem_alloc()` return NULL once inside `blob2items()`, and allocates another list.

Run here, unpatched:

```
SUMMARY: AddressSanitizer: heap-use-after-free /tmp/asan_uaf_<pid>/src/list.c:76 in list_init
```

The seven line references in the body — `list.c:76`, `:91`, `:93`, `:273`, `blob.c:328`, `:334`,
`alloc.c:623` — were each checked against the source independently of the run, and all land
exactly where this bug puts them. The trace in the PR is a condensed form of output we can
regenerate on demand.

## What I am still not asserting

- The mingw-w64 and Wine results in PR 3 come from `repro/mingw_csl.sh` and were **not** re-run
  by me; there is no mingw toolchain on this machine. The script is in the tree and its claims
  are specific enough to be falsified by anyone who runs it. If the sentence in PR 3 cannot be
  stood behind, it should be cut rather than softened.
- The 1000-test suite result likewise comes from the review pass, not from a run here.
