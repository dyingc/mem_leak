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

---

## Round 2: two separate calls (`two_call_probe.py`)

The single-prompt version above lost 3 of 24 previously-accepted functions. Merging the two tasks
into one context is the obvious suspect — a cheap model asked to also hunt elsewhere reasons
differently about what it was actually asked. So split them:

    call 1   the CURRENT production prompt, byte-for-byte (verify.build_prompt + verify.SYSTEM).
             There is no second task in the context, so it cannot regress by construction.
    call 2   fired ONLY when call 1 says false. Fresh context, never shown the numbered reports as
             something to judge: it is told the named allocation was already reviewed and rejected,
             and asked only whether some OTHER allocation in the shown code leaks.

Cost stays bounded because call 2 only runs on rejections.

(Incidentally, round 1's "current" arm was not actually production: it used the *summarize* system
prompt, not `verify.SYSTEM`. `two_call_probe.py` imports `verify.SYSTEM` directly.)

### Results

| | round 1 (one prompt) | round 2 (two calls) |
|---|---|---|
| PR5 recovered | yes | yes — line 313 `ret_free`, conf 0.99, correct failure path |
| call-1 verdict on `parse_type` | false (correct) | false, conf 0.96 (correct) |
| controls flagged | 0/25 | 3/25 — **all three verified real, see below** |
| accepted functions kept | 21/24 | 22/24 |
| cost | $0.066 | $0.093 |

### The noise floor (`noise_floor.py`) — this is what the "lost" counts must be read against

Both probes measure themselves against `llm_verdicts.json`, which holds **one** sample per function.
Re-sampling the *unchanged* production prompt 3× uncached on all 24 accepted functions:

    2/24 functions flip within 3 samples
    7/72 per-sample disagreement with the recorded verdict  (~10%)

    compile_catch        F F F   <- the recorded `true` does not reproduce at all
    get_matches_in_str   F T F
    reserve_local        F T F
    string_reduce        T T T   <- stable

That reverses part of round 1's reading:

- Round 2 lost `get_matches_in_str` and `compile_catch` — both unstable/unreproducible anyway.
  **It lost zero stable functions.**
- Round 1 lost `string_reduce`, which is TTT stable. That was a *real* loss caused by the merged
  prompt (it relocated the same bug into `other_findings`), not noise.
- Round 1's `reserve_local` and `compile_catch` losses were noise, as suspected.

So splitting the calls is the better design on the evidence, not just on principle.

### The 3 controls call 2 flagged are real bugs

"Control" assumed *rejected by the pipeline and untouched by upstream fixes ⇒ clean*. That
assumption is wrong, so this set cannot measure precision. All three check out against the source:

1. **`buf_copy_options` — `buf->b_p_csl` (option.c:7438) is never freed anywhere.**
   `free_buf_options()` (buffer.c:2428) clears ~60 buffer-local string options and omits `b_p_csl`;
   it is the only `b_p_csl` write in the tree. Every re-copy overwrites the old pointer, and buffer
   teardown drops it. Guarded by `#ifdef BACKSLASH_IN_FILENAME`, i.e. **Windows-only** — which is
   why no analyzer here could have seen it: on Linux the line is preprocessed away. The LLM reads
   raw source, so it saw a build we never analyzed. The most substantial of the three.
2. **`split_message` (popupmenu.c:1718) — degenerate `height`.**
   `max_height = Rows / 2 - 1` with no lower bound. At `Rows` 4 or 5, `height` clamps to 1, so
   `(*array)->pum_text` and `(*array + height - 1)->pum_text` are the same slot and the first
   `vim_strsave("")` is dropped. At `Rows <= 3` the second write goes to index -1, which is worse
   than a leak.
3. **`list_filter_map` (list.c:2652) — OOM-only.**
   `list_append_tv_move()` returns FAIL only when `listitem_alloc()` fails, and that `break` is the
   one exit in the loop that does not `clear_tv(&newtv)` first. Same class as PR5.

The honest summary is therefore: 22/25 controls stayed empty, and the 3 non-empty ones were right.
That is evidence against "find something, anything", but it is not a precision measurement.

### What still limits this

- Positive sample is still **n=1**.
- Call 2 can only look inside the shown function. When the *function* is mis-named rather than the
  line, it cannot help — that is 4 of the 9 PRs (`f_setmatches`, `ex_redir`, `ex_class`,
  `serverRegisterName`), where Infer never reported the enclosing function at all.
- A self-reported 0.99 means nothing about stability; see `reserve_local`.

### If this is adopted

`call2` findings need their own provenance in `bugs.json` (e.g. `source: "llm-adjacent"`). Analyzer
warnings have at least passed Z3 path-feasibility; these have been checked by nothing but the model.
