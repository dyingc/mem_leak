# Field note: 11.8 GB memory leak in a live `vim` process (2026-09-06)

Found on this machine while checking resources before running MemHint. Kept here because
Vim 9.2 is the paper's main subject and the leaking code path is C (job/channel refcounting).

## Symptoms

| Item | Value |
|---|---|
| Process | `vi Dockerfile` (PID 9804), `/usr/bin/vim.gtk3`, VIM 9.1 patches 1-948,950-1230 (Debian) |
| Uptime | 55 days (started 2026-07-13), editing a 12 KB Dockerfile, swap file `modified: no` |
| Memory | VmRSS 11.88 GB + 1.45 GB swap; `[heap]` mapping alone 12.58 GiB, 100% private-dirty anon |
| CPU | 11d19h total (21% avg), ~50% of a core at sampling time while "idle" |
| Plugin | `~/.vim/pack/codeium/start/codeium.vim` @ 3c0a4f8 (2026-01-22) |
| Child | Codeium `language_server_linux_x64` (PID 9864, 88 MB RSS — healthy) |

## Mechanism (from strace, gdb, and heap sampling)

1. `autoload/codeium/server.vim:401` — `timer_start(5000, s:SendHeartbeat, {'repeat': -1})`.
2. Every 5 s `codeium#server#Request('Heartbeat', …)` calls `job_start(['curl','-L',uri,'--header',…,'-d@-'], {out_cb, err_cb, exit_cb, close_cb})`
   where all four callbacks are lambdas closing over `result = {'out': [], 'err': []}` and `ExitCallback`.
   Vim writes the 224-byte JSON body to curl's stdin (`ch_sendraw`), curl prints its progress meter
   (469 B on stderr) and the 59-byte reply `{"lastExtensionHeartbeat": …}` on stdout, then exits.
3. strace (8 s window): `clone → execve(curl) → read(stdout/stderr) → SIGCHLD → wait4 → close` once per ~5 s.
   gdb backtrace caught it inside `f_job_start → job_start → mch_job_start → fork`.
4. **Nothing from a finished job is ever released.** Sampling 4×32 MiB windows spread across the 12.58 GiB heap
   via `/proc/PID/mem` gives a uniform density; extrapolated counts over the whole heap:

   | String | Copies in heap |
   |---|---|
   | `{"lastExtensionHeartbeat":…}` (curl stdout) | ~887 K |
   | `% Total    % Received …` (curl progress meter) | ~887 K |
   | `Content-Type: application/json` / `-d@-` (argv) | ~1.77 M (2 copies per job) |
   | `http://127.0.0.1:40371/…/Heartbeat` (uri) | ~2.66 M (3 copies per job) |
   | lambda names in sample: `<lambda>1063016` | → >1 M lambdas created |

   55 days / 5 s ≈ 951 K heartbeats; ≈ 887 K retained jobs ⇒ **~14 KB leaked per `job_start`**, never reclaimed.
   Retained per job: argv list, job + channel structs and read buffers, the `result` dict with stdout/stderr
   strings, four lambda partials and the closure scope (`funccal`) that owns `job`/`channel`/`data`.

5. Likely reason for the rising CPU: Vim's garbage collector periodically marks every reachable list/dict/
   partial; with >1 M retained closure scopes each pass is O(n) and grows with the leak.

## Reading

- Cycle: `job → callbacks (partials) → closure funccal → local var job`. Vim's GC is supposed to break
  such cycles for jobs/channels; either it never runs to completion here, or the job is held by a path the
  collector does not treat as collectable. Worth a minimal reproducer (`job_start` in a 5 s timer with
  closure callbacks, watch RSS) against Vim 9.2.0015 — if it reproduces, it is a candidate for
  cross-checking against MemHint's reports on `src/job.c` / `src/channel.c`.
- Codeium side: `Request()` for heartbeats could reuse one channel or drop the closure-captured callbacks.

## Resolution

`kill -TERM 9804` (Vim's deathtrap preserves the swap file; buffer was unmodified). Language server
terminated with it. ~12 GB RAM and 1.4 GB swap returned to the system.

Evidence artifacts (session scratchpad, not committed): `vim.strace`, gdb backtrace, heap-sampling script.
