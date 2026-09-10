# Pulse OOM, round 3: error traces expand a shared value history exponentially

Rounds 1 and 2 are in `notes/infer-pulse-oom.md` and `notes/infer-pulse-oom-followup.md`. They
found two real problems (summary-cache retention across a file target; no memory checkpoint
reachable from inside an unfinished procedure analysis) and neither of them explained the stress
case the other agent was hitting. This round does.

**It is a bug, not a limit.** Stock Infer v1.2.0 aborts with `Fatal error: out of memory` on a
31-line C file with one procedure and one null-pointer dereference. Upstream fixed it three months
after the commit this build is based on; the fix is backported here, together with a bound that
makes the failure mode impossible rather than merely unlikely.

## 0. The reproducer

The other agent's minimal fixture, reproduced exactly by
`results/infer-oom/tools/gen_history.py --shape doubling --n 24`:

```c
__attribute__((noinline)) int doubling_null(int x) {
  uintptr_t raw = (uintptr_t)0 + ((uintptr_t)x - (uintptr_t)x);
  raw = raw + raw;      /* x 24 */
  ...
  int *p = (int *)raw;
  return *p;
}
```

`raw` is provably 0 throughout, so the abstract state never grows: one value, one disjunct. What
grows is the *history* Pulse keeps so it can explain where the value came from.

## 1. Root cause

`PulseValueHistory.t` is a **DAG**, not a tree. `binary_op bop h h` stores the same `h` as both
children, and more generally any value used twice contributes the same physical sub-history to
both users. After `n` doublings the history has `n` nodes and `2^n` root-to-leaf paths.

Every consumer went through `pop_least_timestamp`, which merges histories in timestamp order by
pushing both children of a `BinaryOp` onto a worklist. Nothing noticed the sharing, so the worklist
grew to one entry per path: time and memory exponential in `n`. `Trace.add_to_errlog` ->
`ValueHistory.add_to_errlog` is where that gets paid, because that is where the history is
materialised into an error trace — which matches, exactly, the stage-level localisation the other
agent did on the real corpus (`add_access_trace` starts and never returns; heap 0.65 GiB -> the
12 GiB address-space limit).

Three measurements pin it down. All are one worker, `--jobs 1 --max-jobs 1`, `nice`/`ionice`,
under a guarded runner; peak RSS is `/usr/bin/time -v`'s maximum resident set.

| stock Infer, same fixture | wall | peak RSS | result |
|---|---|---|---|
| 16 doublings | 1.2 s | 145 MiB | 1 issue |
| 18 | 2.2 s | 187 MiB | 1 issue |
| 20 | 6.2 s | 418 MiB | 1 issue |
| 21 | 12.2 s | 726 MiB | 1 issue |
| 22 | 24.3 s | 1236 MiB | 1 issue |
| 23 | 49.4 s | 2269 MiB | 1 issue |
| **24** | **34.3 s** | **3917 MiB** | **`Fatal error: out of memory`** (4 GiB `RLIMIT_AS`) |

Each extra line doubles both time and memory: that is the signature of walking `2^n` paths.

Two controls separate "big history" from "shared history", and "analysis" from "reporting":

| stock Infer, control | wall | peak RSS | result |
|---|---|---|---|
| `--shape linear --n 24` (24 binary ops, no sharing) | 1.2 s | 106 MiB | 1 issue |
| `--shape fanout --n 24` (balanced tree, no sharing) | 1.2 s | 107 MiB | 1 issue |
| 24 doublings, dereference removed so nothing is reported | 1.2 s | 108 MiB | 0 issues |

Same instruction count, same value, same number of history nodes. The blow-up needs *sharing*, and
it happens *only when the history is turned into a trace* — the analysis itself is linear.

## 2. The fix

`notes/infer-pulse-history-oom.patch`, applied on top of the four earlier patches.

**(a) Traverse the DAG as a DAG.** `pop_least_timestamp` now carries `treated_hists`, the histories
already expanded during the current round, and skips a history that is physically equal to one of
them; `multiplex` drops `Epoch` members and physically-shared duplicates when the node is built.
This is upstream `facebook/infer@c257eb16f` ("[pulse] curb max width of histories", 2024-09-04),
adapted to this tree, which still has `InContext`/`main_only` (upstream removed contexts two days
earlier). It is **lossless**: an already-expanded node contributes exactly the same events again.

**(b) An explicit bound on one trace.** `--pulse-max-trace-elements` (default 10000, `0` disables)
caps how many elements one report's history may contribute. With (a) the bound is a backstop, not
the mechanism — it cannot fire on a shared-DAG shape any more, only on a history that really is
that large. When it fires it is deterministic (the oldest events are dropped, because the
traversal walks backwards from the most recent one) and it says so in the trace itself:

```
earlier history not shown: this value's history reached the 10-element trace limit
(--pulse-max-trace-elements); the issue itself is unaffected
```

and `stats.jsonl` carries `count.backend_stats.pulse_traces_truncated`. Truncating a trace changes
no analysis result: the same issues are reported at the same places with the same types; only the
explanation is shorter, and never silently.

**(c)** `Config.is_running_unit_test` now also recognises `inline-test-runner`, the name dune >= 3
gives the runner. Without it Infer's own argument parser eats the runner's arguments and the
in-tree unit tests cannot run at all.

## 3. Validation

`output/oom/tools/infer-pre` is stock v1.2.0-4c53e80; `infer-hist2` is the fixed build.

**The reproducer.** Under a **2 GiB** `RLIMIT_AS` (half of what stock needed to fail):

| fixed Infer | wall | peak RSS | result |
|---|---|---|---|
| 24 doublings | 1.2 s | 105 MiB | 1 issue, `verdict: COMPLETE` |
| 40 | 1.2 s | 107 MiB | 1 issue |
| 100 | 1.2 s | 107 MiB | 1 issue |
| 1000 (2^1000 paths) | 1.2 s | 115 MiB | 1 issue |

The `NULLPTR_DEREFERENCE` is still reported, on the same line, and its trace is complete: 29
elements, one per assignment, no truncation marker.

**Nothing is lost.** At 20 doublings, where stock still survives, the two `report.json` files are
**identical including every `bug_trace` element**.

**Infer's own Pulse test suites** (`infer/tests/codetoanalyze/{c,cpp}/pulse`, captured and analysed
with both binaries):

| suite | issues | trace elements | identical |
|---|---|---|---|
| C, 248 procedures | 83 -> 83 | 456 -> 456 | yes, byte for byte |
| C++, 293 procedures | 53 -> 53 | 329 -> 329 | same issues; 3 traces reorder events *within one timestamp* |

The C++ difference is three pairs of simultaneous events swapping places (e.g. `variable 'c_true'
declared here` and `variable 'C++ temporary' declared here`, both at the same timestamp). No
element is added or removed and the output is reproducible across runs. Upstream's own commit
refreshed `issues.exp` for cpp/java/objc for the same reason.

**A unit test that fails without the fix.** `PulseValueHistoryTest.ml` gains two cases: 60 levels
of `h ++ h`, and 30 levels of a diamond where the shared history hangs below two different parents.
With the traversal reverted to its old form, the in-tree test runner **aborts with SIGABRT (out of
memory) under `ulimit -v 2 GiB`** on the first of them; with the fix both pass instantly.

**Regressions.** `results/infer-arg-models/verify_patch.sh` 12/12 and `verify_transport.sh` 17/17.
Completeness checks `COMPLETE` on every fixed run above; the stock 24-doubling run is correctly
reported `INCOMPLETE (unexplained gaps)`.

## 4. What this does and does not claim

Proven here: stock Infer aborts on a 31-line generic C file; the cause is exponential traversal of
a shared history during trace materialisation; the fix removes the exponential and changes no
issue and (outside three same-timestamp reorderings) no trace.

Not proven here: that this is the *only* cause of the other agent's production failure. It is the
one their own stage-level localisation pointed at, and it explains the shape of their evidence that
nothing else did — a `load` with `disjuncts_in=1, disjuncts_out=1` growing the heap by 0.95 GiB,
and a 19-versus-20 cliff (one more disjunct means more distinct sub-histories merged into the same
value, which moves the path count over the edge). The way to settle it is to re-run their fixture
on this build.

## 5. Reproducing this

```bash
python3 results/infer-oom/tools/gen_history.py --shape doubling --n 24 --out f.c
infer capture --results-dir cap -- gcc -c f.c -o f.o
( ulimit -v 4194304; infer analyze --results-dir cap --pulse-only --jobs 1 --max-jobs 1 )
```

Stock aborts; the patched build reports one null dereference in about a second. `--shape linear`
and `--shape fanout` are the controls. `output/oom/tools/hist_sweep.sh` (copy in
`results/infer-oom/tools/`) runs the whole sweep through the guarded runner.
