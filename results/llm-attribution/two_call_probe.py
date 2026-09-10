#!/usr/bin/env python3
"""Two *separate* LLM calls instead of one two-tier prompt.

`two_tier_probe.py` put both tasks in one prompt and lost 3 of 24 previously-accepted functions.
One of those (`string_reduce`, the accepted upstream PR) was merely *relocated* into
`other_findings`, but the mechanism is still attention drift: asking a cheap model to also hunt
elsewhere changes how it judges what it was actually asked about.

Splitting removes that coupling by construction:

    call 1   the CURRENT production prompt, byte-for-byte (build_prompt).  Cannot regress except
             through sampling noise -- there is no second task in the context at all.
    call 2   fired ONLY when call 1 says false.  A fresh context that never asks about the numbered
             reports; it is told the reported allocation was already reviewed and rejected, and is
             asked only whether some OTHER allocation in the shown code leaks.

Cost is bounded: call 2 only runs on rejections, and rejections are the cheap half.

Usage:  two_call_probe.py [--n-controls 25]
Writes results/llm-attribution/two_call_results.json
"""
from __future__ import annotations
import argparse, json, random, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memhint.llm import LLM
from memhint.models import Warning
from memhint.verify import SYSTEM, CalleeIndex, FunctionLocator, build_prompt

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output/vim_9_2_0015"
PROJ = ROOT / "subjects/vim_9_2_0015"

SYSTEM2 = ("Role: You are a senior static-analysis engineer specializing in C/C++ memory-safety. "
           "You are auditing a function that a static analyzer flagged, after the specific "
           "allocation it named was reviewed and found not to be a defect.")


def build_prompt2(project: str, func, items, callees: str, tier1_reason: str) -> str:
    """Call 2: no numbered reports to judge, only a search for a different leak."""
    lines = func.code.splitlines()
    bug_lines = {r.warning.line - func.start_line for r in items}
    src = "\n".join(l + ("   // <-- analyzer pointed here" if i in bug_lines else "")
                    for i, l in enumerate(lines))
    pointed = "\n".join(f"  - line {r.warning.line}: {r.warning.message}" for r in items)
    return f"""A static analyzer reported a memory leak in the function below. A reviewer has already
examined the exact allocation the analyzer named and concluded it is NOT a leak:

**Analyzer pointed at:**
{pointed}
**Reviewer's conclusion:** not a defect -- {tier1_reason}

That conclusion is settled. Do not re-examine those allocations and do not argue about them.

Analyzers are more reliable about *whether* a function leaks than about *which* allocation leaks:
the report is often triggered a few lines away from the real defect, or on a value whose ownership
actually moves through a callee. So your one job is different from the reviewer's:

**Is there some OTHER heap allocation in this function that leaks?**

**Project:** {project}
**File:** {func.file}
**Function:** {func.name}

**Function source:**
```c
{src}
```
{callees}
Consider every heap allocation this function makes or receives ownership of, including memory
returned through a pointer-to-pointer out-parameter of a callee. For each, ask whether some
execution path -- especially an early return on an error or allocation failure -- leaves it neither
freed nor stored anywhere the caller can reach.

Report a finding ONLY if you can name (a) the allocating call and the variable holding it, and
(b) the concrete path that drops it, both visible in the code above. Do not report a possibility
you cannot trace. Most functions have no leak; an empty list is the expected and correct answer.

Respond with a single JSON object, no other text:
  {{"findings": [{{"line": <int>, "alloc": "<call and variable>", "path": "<path that drops it>", "confidence": 0.0-1.0}}]}}

Output rules:
  - findings: [] when you find nothing traceable. Never include the allocations listed above.
  - alloc and path: ONE short sentence each.
"""


class R:
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
    return func, items, ctx, build_prompt("vim_9_2_0015", func, items, ctx)


def run_pair(llm, func, items, ctx, base, tag):
    """call 1 verbatim; call 2 only when call 1 rejects."""
    v1 = json.loads(llm.chat(SYSTEM, base, tag=f"2c-c1-{tag}"))
    out = {"call1": v1, "call2": None}
    if not v1.get("verdict"):
        p2 = build_prompt2("vim_9_2_0015", func, items, ctx, str(v1.get("reason", "")))
        out["call2"] = json.loads(llm.chat(SYSTEM2, p2, tag=f"2c-c2-{tag}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-controls", type=int, default=25)
    ap.add_argument("--budget", type=float, default=3.0)
    a = ap.parse_args()

    loc = FunctionLocator(PROJ)
    ci = CalleeIndex(loc, json.loads((OUT / "codebase.json").read_text()))
    llm = LLM(cache_dir=ROOT / "results/llm-attribution/cache", budget_usd=a.budget)
    res = {"positive": None, "controls": [], "tier1_regression": []}

    # ---- positive: PR5 under argn's mis-attribution ---------------------------------------
    c = case("infer-argn", "parse_generic_func_type_args", "src/vim9generics.c", loc, ci)
    if c is None:
        print("PR5 case not found"); return 1
    func, items, ctx, base = c
    print(f"positive: {func.name}  {len(items)} warnings, call-1 prompt {len(base)} chars")
    r = run_pair(llm, func, items, ctx, base, func.name)
    res["positive"] = r
    print(f"  call1 verdict={r['call1'].get('verdict')} conf={r['call1'].get('confidence')} "
          f"reason={str(r['call1'].get('reason'))[:90]}")
    for f_ in ((r["call2"] or {}).get("findings") or []):
        print(f"  call2 FOUND line={f_.get('line')} conf={f_.get('confidence')} "
              f"alloc={str(f_.get('alloc'))[:80]}")
        print(f"        path={str(f_.get('path'))[:110]}")
    if r["call2"] is not None and not (r["call2"].get("findings")):
        print("  call2 found nothing")

    # ---- negative controls -----------------------------------------------------------------
    PR_FUNCS = {"f_setmatches", "barline_parse", "string_reduce", "ex_class",
                "parse_generic_func_type_args", "json_encode_lsp_msg", "ex_redir",
                "gui_gtk_draw_string", "serverRegisterName"}
    verdicts = json.loads((OUT / "infer-argn/llm_verdicts.json").read_text())
    pool = [v for v in verdicts if not v["verdict"] and v["function"] not in PR_FUNCS
            and v["confidence"] >= 0.8]
    random.Random(20260910).shuffle(pool)
    print(f"\n--- {a.n_controls} negative controls (same seed/pool as the single-prompt probe) ---")
    n_flag = 0
    for v in pool[: a.n_controls]:
        c = case("infer-argn", v["function"], v["file"], loc, ci)
        if c is None:
            continue
        func, items, ctx, base = c
        r = run_pair(llm, func, items, ctx, base, func.name)
        fs = ((r["call2"] or {}).get("findings")) or []
        n_flag += bool(fs)
        res["controls"].append({"function": func.name, "file": func.file,
                                "call1_verdict": r["call1"].get("verdict"),
                                "n_findings": len(fs), "findings": fs})
        print(f"  {func.name:34s} call1={str(r['call1'].get('verdict')):5s} call2_findings={len(fs)}")
        for f_ in fs[:2]:
            print(f"      line={f_.get('line')} conf={f_.get('confidence')} {str(f_.get('alloc'))[:66]}")
    n = len(res["controls"])
    res["control_flag_rate"] = f"{n_flag}/{n}"
    print(f"controls flagged by call 2: {n_flag}/{n}")

    # ---- tier-1 regression: identical prompt, so this measures sampling noise only ----------
    print("\n--- call-1 regression (prompt is byte-identical to production) ---")
    kept = lost = 0
    for v in [x for x in verdicts if x["verdict"]]:
        c = case("infer-argn", v["function"], v["file"], loc, ci)
        if c is None:
            continue
        func, items, ctx, base = c
        v1 = json.loads(llm.chat(SYSTEM, base, tag=f"2c-c1-{func.name}"))
        same = bool(v1.get("verdict"))
        kept += same; lost += (not same)
        res["tier1_regression"].append({"function": func.name, "was": True, "now": same,
                                        "idx_was": v["bug_indices"], "idx_now": v1.get("bug_indices")})
        if not same:
            print(f"  LOST  {func.name:34s} {str(v1.get('reason'))[:80]}")
    res["tier1_kept"] = f"{kept}/{kept+lost}"
    print(f"  kept {kept}, lost {lost}")
    res["usage"] = llm.usage.to_dict()
    (ROOT / "results/llm-attribution/two_call_results.json").write_text(json.dumps(res, indent=1))
    print("usage:", llm.usage.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
