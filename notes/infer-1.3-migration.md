# Moving the patches from Infer v1.2.0 to v1.3.0

Status: **in progress** (2026-09-10).  The v1.2.0 toolchain and every number published against it
stay exactly where they are; v1.3.0 is built alongside it and only replaces v1.2.0 once it has been
re-measured end to end.

| | v1.2.0 (current) | v1.3.0 (target) |
| --- | --- | --- |
| commit | `4c53e80ca`, 2024-06-20 | `410a6d939`, 2026-05-12 |
| distance | — | 2158 commits |
| OCaml | 4.14.0+flambda | **5.3.0+flambda** (multicore runtime) |
| dune | 3.24.2 (ours) | 3.17.2 (locked) |
| clang | 15.x | 21.1.6 |
| opam lock | unusable — `cmdliner 1.2.0`, `conf-gmp 4`, `mtime 2.0.0` withdrawn upstream; installed unlocked and pinned by hand | usable, **but only through upstream's own pins** — see below |
| build script | `scripts/build-patched-infer.sh` | `scripts/build-infer-1.3.sh` |
| trees | `tools/infer-src`, `tools/infer` | `tools/infer-src-1.3`, `tools/infer-1.3` |

Both switches live in the same `tools/opam-root`, so nothing about the working v1.2.0 build changes.

## Build recipe: what actually differs

Everything below was established by doing it, not by reading the docs.

* **OCaml 5.3.0+flambda switch** — `opam switch create 5.3.0+flambda
  --package=ocaml-variants.5.3.0+options,ocaml-option-flambda` in the *same* `tools/opam-root`, so
  the working 4.14.0+flambda switch is untouched.  ~20 min.
* **Do not install the dependencies by hand.**  `opam install --deps-only opam/infer.opam.locked`
  fails twice over:
  1. `camlzip = 1.12` — withdrawn from opam-repository (1.11, 1.13, 1.14 remain).  Upstream does not
     use the published package at all: it pins its own fork in `dependencies/camlzip`, which removes
     camlzip's jar-version check.
  2. `charon` — **unknown package**, not on opam-repository in any version.  It is vendored at
     `dependencies/charon` (charon 0.1 / charon-ml, AeneasVerif) and pinned from there.
     Note this is *not* optional even though the Rust analyzer is off by default
     (`configure.ac:170 enable_rust_analyzers_default=no`): `infer/src/integration/dune.in:21`
     lists `charon` in `(libraries …)` unconditionally, so the integration library links it either
     way.
  `./build-infer.sh --only-setup-opam --user-opam-switch -y clang` does the five vendored pins
  (charon, name_matcher_parser, ppx_show, pyml, camlzip), adds the `local-llvm` opam repo and then
  runs the locked install.  Use that.
* **configure flags** — the four `--disable-*-analyzers` we already pass still exist; v1.3.0 adds
  `--disable-rust-analyzers` and `--disable-swift-analyzers` (both already the default, but pass
  them so the build does not change under us).
* **prebuilt clang** — the v1.3.0 release tarball still ships
  `lib/infer/facebook-clang-plugins/{clang/install,libtooling/build}`, so the same symlink +
  `clang/setup.sh --only-record-install` trick works and the 3-hour LLVM build is still avoidable.
  clang goes 15.x → **21.1.6**, which is a real change in the capture front end: the C/C++ the
  fixtures and Vim parse with may differ, so capture has to be re-run, not reused.
* v1.3.0 has no git submodules (neither did v1.2.0); `facebook-clang-plugins` is vendored.

## Per-patch verdict

### 1. `notes/infer-arg-models.patch` — arg-position Pulse models — **still needed, real porting work**

Upstream still offers only `--pulse-model-free-pattern` (frees argument 0) and
`--pulse-model-alloc-pattern` (allocates the *return value*).  Neither
`--pulse-model-free-arg-pattern N:regex` nor `--pulse-model-alloc-arg-pattern N:regex` has an
upstream equivalent, so the whole patch carries over.

What changed underneath it:

* `PulseModelsC.ml` grew 164 → 743 lines; `matchers` moved from line 158 to 537.  The list's shape
  is unchanged (`[ +BuiltinDecl.(match_builtin free) … ] @ ( [ …alloc… ] |> List.map … )`), so the
  two insertion points still exist.
* **The model DSL was renamed.**  `mk_fresh ~model_desc ~more ()` → `fresh ?more ()`,
  `write_deref ~ref ~obj` → `store ~ref`, `start_model` → `start_named_model desc @@ fun () -> …`.
  Our `alloc_out_arg` has to be rewritten against the new names — this is the only part of the
  patch that is not a pure line shift.
* `custom_alloc_not_null` now takes a `desc` string first (`custom_alloc_not_null "custom alloc"`).
* `realloc` / `custom_realloc` gained `~null_case`; does not affect us.

### 2. `notes/infer-argfile-transport.patch` — arguments containing `^` — **still needed, cheapest port**

The bug is untouched upstream: `CommandLineOption.ml:988-1000` still joins sub-process arguments
with `'^'` and still *drops* any argument containing it with
`WARNING: Ignoring unsupported option containing '^' character`, which silently breaks every
anchored regex (`--pulse-model-free-arg-pattern '1:^vim_free$'`).  The file only moved 33+/25−
in two years; the patch fails only on context, at the same three hunks (976→988, 1068→1079).

One new site exists at v1.3.0 `CommandLineOption.ml:1244`, `add_to_env_args`, which appends to
`INFER_ARGS` with the same separator.  Checked all three callers
(`ProcessPool.ml:477` `["--run-as-child"; slot]`, `Config.ml:4595` `["--quiet"]`,
`Config.ml:4602` `["--progress-bar-style"; symbol]`) — all pass `'^'`-free literals, so it needs no
change.  Worth a comment so the next reader does not have to re-derive that.

### 3. `notes/infer-pulse-oom.patch` — summary-cache eviction, compaction unit fix — **half obsolete, half still needed, and one clean upstream bug**

* **Cache eviction — mostly obsolete.**  Upstream now has a generic LRU
  (`Concurrent.Cache.set_lru_mode ~lru_limit`) wired into the summary cache as
  `Summary.OnDisk.set_lru_limit`, with `--summaries-lru-max-size` (default 2000), plus the same for
  attributes, tenvs and inferbo.  **But** `InferAnalyze.ml:162-166` only enables it inside
  `else if Config.multicore then`, and the option help says "Relevant only to multicore mode".  The
  `--jobs 1` path (`run_sequentially`, line 158) and the default forked-`ProcessPool` path (line
  233) never call it, so on exactly the configuration we measure the summary cache is still
  unbounded.  Our port shrinks to *enabling the LRU that already exists* outside multicore.
  Note the difference in kind: upstream's limit counts summaries (2000), ours reacted to heap
  pressure.  Which is the right knob for our workload has to be re-measured — with ~3.8k procedures
  in the big Vim TUs, 2000 is in the right ballpark but was not chosen for this.
* **Compaction threshold unit bug — still present, verbatim.**  It moved from `InferAnalyze.ml` to
  `ProcessPool.ml:322-333`:

  ```ocaml
  Config.compaction_if_heap_greater_equal_to_GB * 1024 * 1024 * (1024 / Sys.word_size_in_bits)
  ...
  L.log_task "Triggering compaction, heap size= %d GB@\n"
    (heap_words * Sys.word_size_in_bits / 1024 / 1024 / 1024) ;
  ```

  `1024 / 64 = 16`, so the documented 8 GB threshold fires at **1 GiB**, and the log line reports
  gigabits as gigabytes — 8× too high.  This is a self-contained upstream defect with no
  dependency on anything of ours: **submit it as its own PR** rather than carrying it privately.
* `--gc-space-overhead` and the explicit `pulse/oom-aborted-procedures-<pid>.txt` +
  `pulse_oom_aborts` / `summary_cache_evictions` counters have no upstream equivalent and carry
  over; `Stats` fields are now `IntCounter.t Atomic.t`, but the `incr Fields.x` idiom is unchanged,
  so only the field *declaration* changes.

### 4. `notes/infer-pulse-oom-followup.patch` — `MemoryPressure`, address-space ceiling, `HeapTrace` — **still relevant, needs an OCaml 5 review**

`--pulse-max-heap` is still opt-in with no default, still checked at exactly one place
(`Pulse.ml:1597` `exec_instr_with_oom_protection_and_path_update`), still `raise_notrace AboutToOOM`
caught at `Pulse.ml:2029`.  So the gap the patch fills — no default protection, one coarse
check per instruction, no address-space ceiling — is unchanged.

How much OCaml 5 actually costs us is smaller than it first looks: `--multicore` defaults to
**false** and its own help text says "[EXPERIMENTAL] … currently partially or not implemented", and
`--unix-fork` still defaults to true on Linux.  So the default analysis path is still forked
workers, exactly what we measure today, and the domain-safety questions below are about not
*breaking* multicore rather than about our own configuration.

Risks introduced by OCaml 5 that must be reviewed, not just recompiled:

* `Gc.compact` was removed in 5.0/5.1 and reinstated in 5.2; 5.3 has it, so the existing
  `Gc.compact ()` calls are fine, but heap statistics are now **per-domain**: `Gc.quick_stat`
  reports the calling domain's minor heap plus the shared major heap.  Under `--multicore` the
  numbers our checkpoints compare are no longer what they were under fork.
* `MemoryPressure.ml` keeps 12 global `ref`s (`procedure_heap_stack`, `peak_heap`, `peak_vm`,
  `aborts`, `last_relief`, `relief_interval_ns`, `compaction_worth_it`, `calls_since_vm_read`,
  …) and `HeapTrace.ml` another 5.  Under forked workers each process gets its own copy, which
  is what they assume; under `--multicore` they would be shared across domains and would have
  to become `DLS` keys (upstream did exactly this to `Ondemand.edges_to_ignore`,
  `gc_stats_pre_spawn`, …), or the port has to state that it is only supported with `--jobs 1` /
  forked workers.
* Hook-site churn to re-find: `ondemand.ml` 428→623 lines (+373/−178), `Payloads.ml` 254→292,
  `AbstractInterpreter.ml` 946→963, `PulseCallOperations.ml` 817→1025, `PulseSummary.ml` 399→417,
  `Pulse.ml` 1894→2037.

### 5. `notes/infer-pulse-history-oom.patch` — value histories are DAGs — **mostly obsolete**

`facebook/infer@c257eb16f` is an ancestor of v1.3.0 (verified), and both halves of our traversal fix
are already there: `PulseValueHistory.ml:200-206` carries `treated_hists` with the
`List.mem ~equal:phys_equal` guard, and `multiplex` already drops `Epoch` members and physically
shared duplicates.  **Do not port that part.**

What remains, and only this:

* `add_to_errlog` (v1.3.0 `PulseValueHistory.ml:463`) still has no bound at all, so the explicit,
  visibly-marked `--pulse-max-trace-elements` backstop still has a job: the DAG fix makes the
  *common* case linear, the budget is what guarantees no pathological history can ever make trace
  materialisation unbounded.  Port the budget, the `Stats.pulse_traces_truncated` counter and the
  two expect-tests.
* `Config.ml:411` still tests only `inline_test_runner` (underscores).  Our dune 3.24.2 names the
  runner with dashes; v1.3.0 pins dune 3.17.2, so **verify at build time** whether the fix is still
  needed before carrying it.

## Order of work

1. Build stock v1.3.0 (`scripts/build-infer-1.3.sh`) and confirm it captures and analyses a C file.
2. Port in order 2 → 5 → 1 → 3 → 4 (cheapest and best-tested first; 4 is the one with real OCaml 5
   design questions).  Compile-check after each.
3. Re-run everything, because **no v1.2.0 number transfers**: `verify_patch.sh` (12),
   `verify_transport.sh` (17), Infer's own C and C++ Pulse suites, the `results/infer-oom` fixtures
   (`doubling/linear/fanout`, guarded runs), the other agent's real OOM reproducer, and the Vim
   corpus as the positive control against the v1.2.0 results.
4. Only then decide whether v1.3.0 becomes the default toolchain.
