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
