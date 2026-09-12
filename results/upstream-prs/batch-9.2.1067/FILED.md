# Filed 2026-09-11

| PR | Title | Branch | Diff |
|----|-------|--------|------|
| [vim/vim#21283](https://github.com/vim/vim/pull/21283) | Fix use-after-free when appending a new list fails | `memhint/list-free-on-failed-append` | `list_free.diff` (+7/-7) |
| [vim/vim#21284](https://github.com/vim/vim/pull/21284) | Fix memory leak in f_setmatches() in src/match.c | `memhint/setmatches-refcount` | `match.diff` + `match_test.diff` (+25/-1) |

Both branch from `90fdb790` (patch 9.2.1067). Commit subjects use `patch 9.2.xxxx:` and neither
touches `version.c`, following vim/vim#21255.

## PR 3 is deliberately held

`buffer_csl.diff` (`b_p_csl` never cleared in `free_buf_options()`) is ready but not filed. It
waits for #21283 to land, not for a number of days.

The reason is not PR volume. It is that #21283 and #21284 can each be checked by the maintainer on
his own machine — `repro/asan_uaf.sh` runs on Linux, and #21284's test fails without the fix in
CI. PR 3 cannot: `BACKSLASH_IN_FILENAME` does not compile there, and a Wine run proving no crash
does not prove no leak. Its real argument is that `free_buf_options()` clears 59 of 60
buffer-local string options and misses this one — persuasive, but something he has to take from
us rather than verify. That credit is worth more after a patch of ours has landed on his reading
of the same garbage-collection machinery.

Acceptance is checked in the tree, not on GitHub: every Vim PR ends as CLOSED, and the maintainer
applies the change as a numbered patch.

```bash
git -C subjects/vim_master fetch origin master
git -C subjects/vim_master log --oneline 90fdb790..FETCH_HEAD --grep=list_free --grep=setmatches
```

## CI outcome

**#21283** — 39 pass, 1 fail. The failure is `codecov/patch`: the seven changed lines have no test
coverage, which is the point the body already makes. All 34 GitHub Actions jobs pass.

**#21284** — 40 pass, 0 fail, on the second attempt.

The first attempt failed one job, `Windows (HUGE, msvc, no, no, x64, conpty)`, in 40 seconds:

```
if_python.c(63): fatal error C1083: Cannot open include file: 'Python.h'
```

Nothing to do with the change — the patch touches `src/match.c` and `src/testdir/test_match.vim`,
and the Windows workflow installs Python 2.7 with `choco install python2 --no-progress`, a network
install that can fail transiently. Re-running the same commit passed, which settles it.

**Re-triggering CI as a fork PR author.** `gh run rerun` fails with "Must have admin rights to
Repository" — a fork's PR author has no admin rights upstream. `.github/workflows/ci.yml` triggers
on `push: branches: ["**"]` and `pull_request:` (whose default types include `reopened`), so
closing and reopening the PR re-runs everything without touching the commit history. A force-push
or an empty commit would also work and leaves a worse trace.
