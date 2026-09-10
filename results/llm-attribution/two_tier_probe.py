#!/usr/bin/env python3
"""Does the LLM stage survive a mis-attributed Infer warning?

Motivation.  On `parse_generic_func_type_args` (upstream PR5) the leak is `ret_free`, allocated at
line 313 by `type_name(type_arg, &ret_free)` and dropped on the `alloc()`-failure early return at
line 327.  Two Infer configurations disagree about *where* to point:

    infer-refE : [1] parse_type@308  [2] type_name@313  [3] type_name@313
    infer-argn : [1] parse_type@308  [2] parse_type@308 [3] parse_type@308

With refE's attribution the LLM found it (verdict true, 0.90, "ret_free is not released when
allocating gt_name fails").  With argn's attribution every run said false at 0.94-0.97, reasoning
correctly about `parse_type`'s ownership and never looking at `ret_free`.

So: can a two-tier prompt recover the bug when the attribution is off by a few lines?

    tier 1  judge exactly what the analyzer points at        (unchanged semantics)
    tier 2  if tier 1 is a false alarm, look for a real leak elsewhere in the shown code
            (the function body and the callee bodies we already send), reported separately

The obvious risk is that tier 2 turns into "find something, anything" and destroys precision, so
this probe runs negative controls as well: functions the current pipeline rejected and that no
upstream fix touches.  A tier-2 hit rate near 100% on those means the second tier is noise.

Usage:  two_tier_probe.py [--n-controls 8]
Writes results/llm-attribution/probe_results.json
"""
from __future__ import annotations
import argparse, json, random, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memhint.llm import LLM
from memhint.models import Warning
from memhint.verify import CalleeIndex, FunctionLocator, build_prompt

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output/vim_9_2_0015"
PROJ = ROOT / "subjects/vim_9_2_0015"

SYSTEM = ("You are a memory safety expert analyzing C/C++ code to identify function semantics "
          "that help static analyzers detect memory leaks.")

TIER2 = """
The static analyzer points at a specific allocation and line. Its *judgement* is often right while
its *attribution* is off by a few lines or names the wrong allocation, so answer two questions.

1. FIRST, exactly as before: is any numbered reported issue a real leak, taken at the location and
   allocation the analyzer names?  This decides "verdict" and "bug_indices".

2. THEN, and only using the code shown above (this function body and the callee bodies), ask
   whether some OTHER heap allocation visible in this function leaks on some path -- typically an
   allocation a few lines away from the reported one, or one reached through a callee that hands
   ownership back. Report those in "other_findings", each as
       {"line": <line number in this function>, "alloc": "<what was allocated and by what call>",
        "path": "<the path on which it is neither freed nor ownership-transferred>"}
   Include a finding here ONLY if you can name the allocation, the variable, and the concrete path
   that drops it. Do not list a possibility you cannot trace in the shown code. An empty list is
   the correct answer for a function with no leak; most functions have none.
   Do NOT put these in "bug_indices" -- they are not what the analyzer reported.
"""


def two_tier_prompt(base: str) -> str:
    """Insert the tier-2 task and extend the JSON schema of the existing prompt."""
    anchor = "Does this function actually have a memory leak"
    head, tail = base.split(anchor, 1)
    tail = anchor + tail
    tail = tail.replace(
        '  {{"verdict"', '  {{"verdict"')  # no-op, keep formatting stable
    tail = tail.replace(
        '{"verdict": true | false, "confidence": 0.0-1.0, "reason": "one short sentence", "bug_indices": [1] or [2,3] or []}',
        '{"verdict": true | false, "confidence": 0.0-1.0, "reason": "one short sentence", '
        '"bug_indices": [1] or [2,3] or [], "other_findings": []}')
    tail = tail.replace(
        "  - reason: ONE short sentence only. Do not quote code.",
        "  - reason: ONE short sentence only. Do not quote code.\n"
        "  - other_findings: [] unless you can name allocation, variable and dropping path.")
    return head + TIER2 + "\n" + tail


class R:
    """Shim with the two attributes build_prompt()/CalleeIndex.context() read."""
    def __init__(self, d):
        self.warning = Warning.from_dict(d["warning"])
        self.path_lines = d.get("path_lines", [])


def case(run: str, func_name: str, file: str, loc, ci):
    z3 = json.loads((OUT / run / "z3_results.json").read_text())
    items = [R(d) for d in z3 if d.get("function") == func_name and d.get("feasible")]
    func = next((f for f in loc.functions_in(file) if f.name == func_name), None)
    if func is None or not items:
        return None
    ctx = ci.context(func, items, 50, 1000)
    base = build_prompt("vim_9_2_0015", func, items, ctx)
    return func, items, base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-controls", type=int, default=8)
    ap.add_argument("--budget", type=float, default=3.0)
    a = ap.parse_args()

    loc = FunctionLocator(PROJ)
    cb = json.loads((OUT / "codebase.json").read_text())
    ci = CalleeIndex(loc, cb)
    llm = LLM(cache_dir=ROOT / "results/llm-attribution/cache", budget_usd=a.budget)

    results = {"positive": {}, "controls": []}

    # ---- positive case: PR5 under argn's (mis-attributed) warning set ----------------------
    c = case("infer-argn", "parse_generic_func_type_args", "src/vim9generics.c", loc, ci)
    if c is None:
        print("PR5 case not found"); return 1
    func, items, base = c
    print(f"positive case: {func.name}  {len(items)} warnings, prompt {len(base)} chars")
    for label, prompt in (("current", base), ("two_tier", two_tier_prompt(base))):
        out = json.loads(llm.chat(SYSTEM, prompt, tag=f"probe-{label}-{func.name}"))
        results["positive"][label] = out
        print(f"  {label:9s} verdict={out.get('verdict')} conf={out.get('confidence')} "
              f"idx={out.get('bug_indices')} other={len(out.get('other_findings') or [])}")
        print(f"            reason: {out.get('reason','')[:120]}")
        for f_ in (out.get("other_findings") or []):
            print(f"            OTHER  line={f_.get('line')} alloc={str(f_.get('alloc'))[:70]}")
            print(f"                   path={str(f_.get('path'))[:100]}")

    # ---- negative controls: functions the pipeline rejected, no upstream fix ---------------
    PR_FUNCS = {"f_setmatches", "barline_parse", "string_reduce", "ex_class",
                "parse_generic_func_type_args", "json_encode_lsp_msg", "ex_redir",
                "gui_gtk_draw_string", "serverRegisterName"}
    verdicts = json.loads((OUT / "infer-argn/llm_verdicts.json").read_text())
    pool = [v for v in verdicts if not v["verdict"] and v["function"] not in PR_FUNCS
            and v["confidence"] >= 0.8]
    random.Random(20260910).shuffle(pool)
    n_flag = 0
    for v in pool[: a.n_controls]:
        c = case("infer-argn", v["function"], v["file"], loc, ci)
        if c is None:
            continue
        func, items, base = c
        out = json.loads(llm.chat(SYSTEM, two_tier_prompt(base), tag=f"probe-ctl-{func.name}"))
        others = out.get("other_findings") or []
        n_flag += bool(others)
        results["controls"].append({"function": func.name, "file": func.file,
                                    "verdict": out.get("verdict"), "n_other": len(others),
                                    "other_findings": others, "reason": out.get("reason")})
        print(f"  control {func.name:32s} verdict={out.get('verdict')} other={len(others)}")
        for f_ in others[:2]:
            print(f"            OTHER line={f_.get('line')} {str(f_.get('alloc'))[:60]}")
    n = len(results["controls"])
    print(f"\ncontrols flagged something in tier 2: {n_flag}/{n}")
    results["control_flag_rate"] = f"{n_flag}/{n}"

    # ---- tier-1 regression: functions the current pipeline ACCEPTED must stay accepted ------
    print("\n--- tier-1 regression on the functions the current prompt said true ---")
    kept = flipped = 0
    results["tier1_regression"] = []
    for v in [x for x in verdicts if x["verdict"]]:
        c = case("infer-argn", v["function"], v["file"], loc, ci)
        if c is None:
            continue
        func, items, base = c
        out = json.loads(llm.chat(SYSTEM, two_tier_prompt(base), tag=f"probe-t1-{func.name}"))
        same = bool(out.get("verdict"))
        kept += same; flipped += (not same)
        results["tier1_regression"].append({"function": func.name, "was": True, "now": same,
                                            "idx_was": v["bug_indices"], "idx_now": out.get("bug_indices"),
                                            "n_other": len(out.get("other_findings") or [])})
        if not same:
            print(f"  LOST  {func.name:34s} {str(out.get('reason'))[:80]}")
    print(f"  kept {kept}, lost {flipped}")
    results["tier1_kept"] = f"{kept}/{kept+flipped}"
    results["usage"] = llm.usage.to_dict()
    (ROOT / "results/llm-attribution/probe_results.json").write_text(json.dumps(results, indent=1))
    print("usage:", llm.usage.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
