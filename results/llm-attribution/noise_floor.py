#!/usr/bin/env python3
"""How much of a 'regression' is just sampling noise?

Both probes measure themselves against `llm_verdicts.json`, a *single* sample per function. If the
production prompt itself flips verdicts between samples, a few lost functions prove nothing about
the prompt change. So: re-sample the byte-identical production prompt N times, uncached, on every
function the pipeline accepted, and count flips.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memhint.llm import LLM
from memhint.verify import SYSTEM, CalleeIndex, FunctionLocator
from two_call_probe import case  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output/vim_9_2_0015"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

loc = FunctionLocator(ROOT / "subjects/vim_9_2_0015")
ci = CalleeIndex(loc, json.loads((OUT / "codebase.json").read_text()))
llm = LLM(cache_dir=None, budget_usd=3.0)          # no cache: every sample is fresh
verdicts = json.loads((OUT / "infer-argn/llm_verdicts.json").read_text())

rows, unstable = [], 0
for v in [x for x in verdicts if x["verdict"]]:
    c = case("infer-argn", v["function"], v["file"], loc, ci)
    if c is None:
        continue
    func, items, ctx, base = c
    s = [bool(json.loads(llm.chat(SYSTEM, base, tag=f"noise-{func.name}-{i}")).get("verdict"))
         for i in range(N)]
    stable = all(s) or not any(s)
    unstable += not stable
    rows.append({"function": func.name, "recorded": True, "samples": s, "stable": stable})
    print(f"  {func.name:34s} {['T' if x else 'F' for x in s]}  {'' if stable else '<-- UNSTABLE'}")

print(f"\n{unstable}/{len(rows)} functions flip within {N} samples of the *unchanged* prompt")
flat = [x for r in rows for x in r["samples"]]
print(f"per-sample disagreement with the recorded verdict: {sum(1 for x in flat if not x)}/{len(flat)}")
(ROOT / "results/llm-attribution/noise_floor.json").write_text(
    json.dumps({"n_samples": N, "rows": rows, "unstable": unstable, "total": len(rows),
                "usage": llm.usage.to_dict()}, indent=1))
print("usage:", llm.usage.to_dict())
