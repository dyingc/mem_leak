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

Applied in that order they reproduce `tools/infer-src-1.3` byte for byte.

Upstream's own Pulse suites are unaffected: `make -C infer/tests/codetoanalyze/c/pulse test` and
`.../cpp/pulse test` both pass with **no diff** against upstream's `issues.exp`.  (On v1.2.0 the C++
suite had three traces whose same-timestamp events reordered; upstream has since refreshed its own
expectations, so on v1.3.0 there is nothing left to explain away.)

Not yet ported: `infer-pulse-oom.patch` and `infer-pulse-oom-followup.patch` (patches 3 and 4).
