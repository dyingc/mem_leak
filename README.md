# MemHint reproduction

Independent re-implementation of **"Finding Memory Leaks in C/C++ Programs via Neuro-Symbolic
Augmented Static Analysis"** (Huang, Shi, Wang, Yang, Lo — [arXiv 2603.27224](https://arxiv.org/html/2603.27224v4)),
run on Vim 9.2.0015 with `gpt-5.6-luna` in place of Gemini.

```
Stage 1  extract.py     Tree-sitter: functions, function-like macros, pointer typedefs, callees; pointer pre-filter
         summarize.py   LLM classifies 20 functions/call (paper prompt A-A) -> {name, role, target}
         analysis.py    Z3 validation of every summary on the function's CFG (paper Eq. 1-2, Fig. 4)
Stage 2  analyzers/     CodeQL data-extension model pack / Infer Pulse alloc+free patterns (paper Appendix B)
Stage 3  analysis.py    Z3 path feasibility of each warning: reach ∧ alloc ∧ ¬freed ∧ ¬escaped (Eq. 3-5, Fig. 5)
         verify.py      LLM validation, one function per call (paper prompt A-B)
Eval     evaluate.py    upstream leak-fix commits after the analysed tag as ground truth
         report.py      Markdown report against the paper's Tables I/II/IV/V
```

Shared symbolic core: `cfg.py` (acyclic CFG: if/switch/goto/return, loops unrolled once, `#if` as a branch) and
`symbolic.py` (edge variables, AtMost-1 incoming edge, state propagation; branch conditions are uninterpreted
Booleans — shared between branch nodes only when every variable in the condition has a single definition in
the function, so `if (x) … if (!x)` correlate but `item = f(); if (item) … item = g(); if (item)` do not;
`p == NULL` after `p = alloc()` clears `alloc`).

Deviations from the paper worth knowing: warnings are deduplicated across the two CodeQL queries (the paper's
#Warn. counts appear to include duplicates from its extra enhanced queries), Infer runs with default Pulse limits
(`--pulse-max-disjuncts 20`, which drops >1M disjuncts on Vim), and the LLM is gpt-5.6-luna for both phases.

## Setup

```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python \
    "tree-sitter>=0.23" tree-sitter-c tree-sitter-cpp z3-solver==4.15.4.0 openai pyyaml tqdm pytest
# tools/codeql  = CodeQL bundle v2.23.9 (github/codeql-action releases, includes cpp-queries)
# tools/infer   = Infer v1.2.0
export OPENAI_API_KEY=...        # only gpt-5.6-luna is used
.venv/bin/python -m pytest -q    # Fig. 4 (a)-(d), Fig. 5, delegation/alias/macro cases
```

## Run (Vim)

```bash
git clone --branch v9.2.0015 --depth 1 https://github.com/vim/vim subjects/vim_9_2_0015
P=subjects/vim_9_2_0015; O=output/vim_9_2_0015
.venv/bin/python -m memhint stage1 --project $P --out $O               # extract + LLM + Z3  (~12 min, ~$1.7)
# build once: CodeQL database and Infer capture (see output/vim_9_2_0015/build_dbs.sh)
(cd $P && codeql database create ../../$O/codeql-db --language=cpp --command="make -j8")
(cd $P && make clean && infer capture --results-dir ../../$O/infer-out -- make -j8)
for A in codeql infer; do
  .venv/bin/python -m memhint stage2 --project $P --out $O --analyzer $A            # with summaries
  .venv/bin/python -m memhint stage2 --project $P --out $O --analyzer $A --vanilla  # baseline
  .venv/bin/python -m memhint stage3 --project $P --out $O --analyzer $A
  .venv/bin/python -m memhint stage3 --project $P --out $O --analyzer $A --vanilla
done
(cd $P && git fetch --filter=blob:none --shallow-since=2025-12-01 origin master)
.venv/bin/python -m memhint.evaluate $P $O v9.2.0015 FETCH_HEAD    # ground_truth.md
.venv/bin/python -m memhint.report $O > $O/REPORT.md
```

Every LLM response is cached under `output/<project>/llm_cache/`, so re-running a stage is free.

## Outputs

| file | content |
|---|---|
| `codebase.json`, `stage1_stats.json` | Phase 1 extraction and the Table-IV funnel |
| `hints_raw.json`, `hints.json`, `hints_rejected.json` | LLM summaries, Z3-validated summaries (paper Appendix B format), rejections with reasons |
| `codeql-ext/` | generated CodeQL model pack (`allocationFunctionModel` / `deallocationFunctionModel`) |
| `<analyzer>[-vanilla]/warnings.json` | normalised analyzer warnings |
| `<analyzer>[-vanilla]/z3_results.json` | Phase 5 verdict + reason + witness path per warning |
| `<analyzer>[-vanilla]/llm_verdicts.json`, `bugs.json` | Phase 6 verdicts and final reported bugs |
| `ground_truth.md/json`, `manual_review.json` | upstream-fix matching and manual review of the rest |
| `REPORT.md` | everything above next to the paper's numbers |

`notes/vim-codeium-heartbeat-leak.md` documents a real 12 GB leak found on the development machine while setting up.
