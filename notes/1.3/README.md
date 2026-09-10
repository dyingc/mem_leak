# v1.3.0 ports of the five patches

Work in progress.  `notes/*.patch` are the v1.2.0 originals and stay authoritative until v1.3.0 is
fully re-measured; the files here are what each one becomes on top of `410a6d939`.

How each original behaves against v1.3.0 (`patch -p1 --fuzz=3` on a pristine `git archive` tree):

| patch | result |
| --- | --- |
| `infer-argfile-transport.patch` | all 4 hunks apply, offset +8, no fuzz — pure rebase |
| `infer-arg-models.patch` | `Config.ml` 5/5 apply (offsets −46…+138); `Config.mli` 1 hunk rejected (upstream inserted a `val` between two context lines); `PulseModelsC.ml` 3/3 "apply" only with fuzz 2–3 and would not compile anyway — the model DSL was renamed |
| `infer-pulse-oom.patch` | needs restructuring: the compaction site moved to `ProcessPool.ml`, the cache LRU now exists upstream |
| `infer-pulse-oom-followup.patch` | hook sites moved substantially; new files carry over |
| `infer-pulse-history-oom.patch` | the traversal fix is already upstream — only the trace budget is ported, by `port5-trace-budget.py` |

`port5-trace-budget.py` is run from the v1.3.0 source root and is idempotent.

## Status (2026-09-10)

Ported, built and verified against v1.3.0 (`410a6d939`), OCaml 5.3.0+flambda:

| ported patch | what it is | verification |
| --- | --- | --- |
| `infer-argfile-transport.patch` | arguments containing `^` reach sub-processes intact | `verify_transport.sh` **17/17** |
| `infer-pulse-trace-budget.patch` | `--pulse-max-trace-elements` + `pulse_traces_truncated` counter + two DAG-sharing unit tests | `dune build @src/pulse/unit/runtest` passes and is discriminating (a deliberately wrong expectation is reported as a diff); with `--pulse-max-trace-elements 5` the trace is cut to the marker + 5 elements, the issue is still reported, and the counter goes 0 → 1 |
| `infer-arg-models.patch` | `--pulse-model-{free,alloc}-arg-pattern` | `verify_patch.sh` **12/12** |
| `infer-pulse-memory.patch` | compaction unit fix in both pools, the summary LRU actually bounded outside multicore, `--gc-space-overhead` | see below |

Applied in that order (transport, trace-budget, arg-models, memory) they reproduce
`tools/infer-src-1.3` byte for byte.

Upstream's own Pulse suites are unaffected: `make -C infer/tests/codetoanalyze/c/pulse test` and
`.../cpp/pulse test` both pass with **no diff** against upstream's `issues.exp`.  (On v1.2.0 the C++
suite had three traces whose same-timestamp events reordered; upstream has since refreshed its own
expectations, so on v1.3.0 there is nothing left to explain away.)

### What `infer-pulse-memory.patch` is, and the two upstream bugs it fixes

Most of v1.2.0's `infer-pulse-oom.patch` is obsolete: upstream grew its own summary LRU.  What
survives is the compaction unit fix, `--gc-space-overhead`, and *making the LRU upstream already has
actually work*.  Two separate upstream defects had to be fixed for that, both measured:

**1. The compaction threshold is 8x too small, and the log line 8x too large.**  Present verbatim in
`ProcessPool.ml:322` (forked workers) and `DomainPool.ml:176` (multicore): they multiply GB by
`1024 / Sys.word_size_in_bits` = 16 instead of `1024 / (word_size_in_bits / 8)` = 128.  Demonstrated
on the 800-procedure fixture, same workload, `--jobs 2 --compaction-if-heap-greater-equal-to-GB 1`:

| | compactions | log line for the same heap |
| --- | --- | --- |
| stock v1.3.0 | 1 (it thinks 128 MiB is 1 GB) | `heap size= 1 GB` |
| fixed | 0 | `heap size= 0 GB` |

The default is dropped 8 -> 1 at the same time, so the *effective* threshold stays at the 1 GiB it
has always really been rather than silently becoming 8x laxer.

**2. `--summaries-lru-max-size` never did anything, in any mode.**  `Concurrent.MakeCache` applies
`lru_limit` only in `add`; `Summary.OnDisk` inserts through `update` (it keeps a two-layer
procname -> AnalysisRequest.Map -> summary structure), which had no trimming at all.  So the summary
cache was unbounded even in multicore mode, the one place upstream switches the limit on.  Measured
on the 800-procedure fixture, summary-cache hit rate:

| `--summaries-lru-max-size` | before the fix | after |
| --- | --- | --- |
| 0 (unbounded) | 64% | 64% |
| 2000 (default) | 64% | 64% |
| 100 | 64% | **41%** |
| 8 | 64% | **41%** |

The control that told us the mechanism itself was fine: `--attributes-lru-max-size 1`, which inserts
through `add`, already moved the attributes hit rate 98% -> 81% before the fix.

Bounding the cache is **lossless**: an evicted summary is written to the results database before
being cached, so it is re-read on its next use.  All four limits above produce identical reports on
the fixture, and upstream's C and C++ Pulse suites still pass with no diff.

On this fixture the bound buys no memory (peak RSS 291-304 MB at every limit): 800 small summaries
are not what fills the heap.  That is expected -- the v1.2.0 evidence for the cache mattering came
from Vim translation units, where the cache reached ~1.3 GiB of a 3.06 GiB heap.  Re-measuring that
on v1.3.0 is still to do.

Not yet ported: the diagnostics half of `infer-pulse-oom.patch` (`HeapTrace.ml`, the explicit
`pulse/oom-aborted-procedures-<pid>.txt` list) and all of `infer-pulse-oom-followup.patch`
(`MemoryPressure.ml`, the address-space ceiling).
