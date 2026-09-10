# Can the LLM stage survive a mis-attributed analyzer warning?

Probe: `two_tier_probe.py`, raw output `probe_results.json`, prompt cache `cache/`.
Model `gpt-5.6-luna`, total cost of the whole experiment **$0.066**.

## Why

`parse_generic_func_type_args` (upstream PR5, `notes/../results/upstream-prs`) leaks `ret_free`,
allocated at line 313 by `type_name(type_arg, &ret_free)` and dropped on the `alloc()`-failure
early return at line 327.  Two Infer configurations disagree about **where to point**:

```
infer-refE : [1] parse_type@308   [2] type_name@313   [3] type_name@313  -> LLM: true,  0.90, "ret_free ... when allocating gt_name fails"
infer-argn : [1] parse_type@308   [2] parse_type@308  [3] parse_type@308 -> LLM: false, 0.97, "the type is owned by gfat_arg_types"
```

Both LLM answers are *locally correct*: `parse_type`'s result really is owned by the table.  The
run that found the bug is simply the one whose warning pointed at line 313.  So the LLM stage's
recall depends on the analyzer's attribution being right to within a few lines, which is exactly
the thing an abstract interpreter is least reliable about.

## What was tried

A two-tier prompt on top of the existing one (the function body and callee bodies are already in
the prompt, so no extra context is needed):

1. **tier 1** — unchanged: is any *numbered reported issue* real, at the location and allocation
   the analyzer names?  This still decides `verdict` / `bug_indices`.
2. **tier 2** — new: if some *other* heap allocation visible in the shown code leaks, report it in
   `other_findings` with the line, the allocation and the dropping path.  Explicitly **not** merged
   into `bug_indices`, and gated on being able to name allocation + variable + concrete path.

## Results

| | outcome |
| --- | --- |
| **PR5 recovered from the bad attribution** | tier 1 still says false (correctly, about `parse_type`); **tier 2 finds `ret_free` at line 313** with the right path: *"if allocation of `generic_arg->gt_name` at line 324 fails, the function returns"* |
| **False-alarm cost** | **0 / 25** control functions produced any `other_findings`.  The second tier stayed silent on every function the pipeline had rejected. |
| **tier-1 stability** | 21 / 24 previously-accepted functions kept |

The three that changed, examined one by one:

* **`string_reduce` (PR3, the one accepted upstream as patch 9.2.1058)** — not lost, *relocated*:
  tier 2 reports the same `funccall_T` from line 1039, *"After `eval_expr_typval` returns FAIL …
  `string_reduce` returns before `remove_funccal(fc)`"*.  It only moved from `bug_indices` to
  `other_findings`, so a pipeline that consumes both keeps it.
* **`reserve_local`** — LLM noise, not a prompt effect: re-sampling the **current** prompt on it
  gives `false, 0.98`, i.e. the original `true` was not stable either.
* **`compile_catch`** — could not be re-sampled (no feasible Z3 result for it under `infer-argn`),
  so unverified.

## Reading

On the ground truth available here the change is a net gain: **+1 real leak recovered (PR5),
0 false alarms in 25 controls, and the one accepted upstream patch (PR3) preserved** — provided the
pipeline treats `other_findings` as findings rather than dropping them.

Caveats, all real:

* One positive case.  It is the right one (a known upstream-confirmed leak that the current
  prompt provably misses) but it is still n=1 for the gain.
* `other_findings` are **function-local**.  When the attribution is wrong about the *function*
  rather than the line, the second tier cannot help, because the prompt only ever contains the one
  reported function.  Four of the nine PRs are exactly that case (`f_setmatches`, `ex_redir`,
  `ex_class`, `serverRegisterName` are never reported by Infer at all).
* Single samples per function; the `reserve_local` result shows this model flips on borderline
  cases at 0.98 stated confidence, so the stated confidence is not a reliable stability signal.

## If this is adopted

`other_findings` need a place in `bugs.json` with a distinct provenance (`source: "llm-adjacent"`
rather than `analyzer`), because their false-positive profile is different from an analyzer
warning that Z3 has already checked: nothing has verified the path except the model itself.
