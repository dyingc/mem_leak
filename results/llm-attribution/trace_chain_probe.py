#!/usr/bin/env python3
"""Give the LLM the source at the lines Infer's trace actually points at.

Infer's `bug_trace` is nested: for `serverSendToVim` the control arm hands us

    lvl=0 if_xcmdsrv.c:413  allocation part of the trace starts here
    lvl=1 if_xcmdsrv.c:413  when calling `LookupName` here
    lvl=2 if_xcmdsrv.c:954  allocated by `vim_strsave (custom malloc)` here
    lvl=0 if_xcmdsrv.c:427  memory becomes unreachable here

Line 954 is `*loose = vim_strsave(p + 1);` -- exactly the conditional allocation the model kept
denying (0/6 and 1/6 across two arms, always with the same wrong claim that LookupName only
allocates when it finds a window). The number 954 is already in the prompt as trace text, but the
callee bodies are dumped unnumbered, so the pointer dangles: there is nothing in the shown code
for "954" to refer to.

CalleeIndex today is name-based -- it scrapes callee names out of the warning text and the
reported function's body, then sends up to 50 whole bodies clipped from the top. It never uses the
trace's (file, line) pairs. This probe replaces that with the call chain the trace names, excerpted
around those lines, with absolute line numbers, and the trace lines marked.

Variants:
    current  what the pipeline sends today (all callees, whole bodies, no line numbers)
    chain    only the functions the trace descends into, excerpted and numbered
    both     chain excerpts in addition to the current callee bodies
`both` separates "the line numbers helped" from "dropping the unrelated callees helped".
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memhint.llm import LLM, parse_json
from memhint.models import Warning
from memhint.verify import SYSTEM, CalleeIndex, FunctionLocator, build_prompt

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output/vim_9_2_0015"
PROJ = ROOT / "subjects/vim_9_2_0015"


class R:
    def __init__(self, d):
        self.warning = Warning.from_dict(d["warning"])
        self.path_lines = d.get("path_lines", [])


WHOLE_UPTO, HALF_WINDOW = 200, 100


def chain_context(loc: FunctionLocator, func, items,
                  whole_upto: int = WHOLE_UPTO, half_window: int = HALF_WINDOW) -> str:
    """Source of every function the trace descends into, outside the reported function.

    Measured on this corpus, a chain callee is 17 lines at the median and 96.6% are under 200, so
    the default is to show the whole function and cut nothing; only the rare large one is clipped,
    to +/-100 lines centred on the trace line. That sidesteps choosing a window: at a 6-sample
    resolution the fixed windows 8/15/20/40 were indistinguishable from each other and from noise,
    while +/-40 (which covered every relevant line of LookupName) scored the same as +/-8 (which cut
    the guard away). Lines carry their real file line numbers so the "line 954" in the trace text
    has something in the shown code to point at, and the trace line itself is marked.
    """
    seen, blocks = set(), []
    for r in items:
        for st in r.warning.trace:
            f, ln = st.get("file"), st.get("line")
            if not f or not ln:
                continue
            if f == func.file and func.start_line <= ln <= func.end_line:
                continue                      # already shown in the reported function's body
            owner = loc.at(f, ln)
            if owner is None or (owner.file, owner.name) in seen:
                continue
            seen.add((owner.file, owner.name))
            lines = owner.code.splitlines()
            rel = ln - owner.start_line
            if len(lines) <= whole_upto:
                lo, hi = 0, len(lines)
            else:
                lo, hi = max(0, rel - half_window), min(len(lines), rel + half_window + 1)
            body = []
            if lo > 0:
                body.append(f"      /* ... {lo} earlier lines of {owner.name}() omitted ... */")
            for i in range(lo, hi):
                n = owner.start_line + i
                mark = "  // <-- trace: " + st.get("message", "") if i == rel else ""
                body.append(f"{n:6d}  {lines[i]}{mark}")
            if hi < len(lines):
                body.append(f"      /* ... {len(lines) - hi} later lines omitted ... */")
            blocks.append(f"// {f}  --  {owner.name}(), around line {ln}\n" + "\n".join(body))
    if not blocks:
        return ""
    return ("\n**The analyzer's trace descends into these functions. Source shown with real line "
            "numbers, centred on the line the trace names:**\n```c\n" + "\n\n".join(blocks) + "\n```\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="infer-v13argn")
    ap.add_argument("--function", default="serverSendToVim")
    ap.add_argument("-n", type=int, default=6)
    ap.add_argument("--truth", choices=["true", "false"], default="true")
    a = ap.parse_args()

    loc = FunctionLocator(PROJ)
    ci = CalleeIndex(loc, json.loads((OUT / "codebase.json").read_text()))
    llm = LLM(cache_dir=None, budget_usd=3.0)          # uncached: every sample fresh

    z3 = json.loads((OUT / a.run / "z3_results.json").read_text())
    items = [R(d) for d in z3 if d.get("function") == a.function and d.get("feasible")]
    if not items:
        print(f"no feasible warning for {a.function} in {a.run}"); return 1
    func = loc.at(items[0].warning.file, items[0].warning.line)
    cur = ci.context(func, items, 50, 1000)
    chain = chain_context(loc, func, items)
    if not chain:
        print("the trace never leaves the reported function; nothing to test"); return 1

    variants = {"current": cur, "chain": chain, "both": cur + chain}
    truth = a.truth == "true"
    print(f"{a.function} in {a.run}, truth={truth}, {a.n} uncached samples per variant\n")
    res = {}
    for name, ctx in variants.items():
        p = build_prompt("vim_9_2_0015", func, items, ctx)
        got = []
        for i in range(a.n):
            d = parse_json(llm.chat(SYSTEM, p, tag=f"chain-{name}-{i}"))
            got.append((bool(d.get("verdict")), d.get("confidence"), str(d.get("reason", ""))))
        n_ok = sum(1 for v, _, _ in got if v == truth)
        res[name] = {"prompt_chars": len(p), "correct": f"{n_ok}/{a.n}",
                     "samples": [{"verdict": v, "confidence": c, "reason": r} for v, c, r in got]}
        print(f"=== {name:8s} prompt {len(p):6d} chars   correct {n_ok}/{a.n}   "
              f"{['T' if v else 'F' for v, _, _ in got]}")
        for v, c, r in got:
            print(f"      {'T' if v else 'F'} {c}  {r[:100]}")
    res["usage"] = llm.usage.to_dict()
    (ROOT / "results/llm-attribution/trace_chain_results.json").write_text(json.dumps(res, indent=1))
    print("\nusage:", llm.usage.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
