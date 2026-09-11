#!/usr/bin/env python3
"""Full A/B of trace-chain callee context, with a null group.

`current`  what the pipeline sends today: up to 50 callees picked by name from the warning text and
           the reported function's body, whole bodies, no line numbers, nothing marked.
`chain`    only the functions Infer's own bug_trace descends into, whole body when it is under 200
           lines (the median chain callee is 17), otherwise +/-100 lines around the trace line, with
           real file line numbers and the trace line marked.

Three samples per cell and a majority vote, because a single sample decides nothing here: the same
prompt on `serverSendToVim` scored 4/6 one hour and 2/6 the next, and the measured per-sample
disagreement on this pipeline is around 10%.

A majority flip is only evidence if majorities are stable without any change at all, so a null
group of functions the change cannot touch (their trace never leaves the reported function) is run
twice through `current` and its flip rate is the floor to read everything else against.

Verdicts here are not ground truth. This measures how much the context change moves the model; only
reading the source of the flipped functions can say whether it moved it the right way.
"""
from __future__ import annotations
import argparse, json, random, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trace_chain_probe import chain_context, R
from memhint.llm import LLM, parse_json
from memhint.models import Warning
from memhint.verify import SYSTEM, CalleeIndex, FunctionLocator, build_prompt

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output/vim_9_2_0015"


def majority(votes: list[bool]) -> bool:
    return Counter(votes).most_common(1)[0][0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="infer-v13argn")
    ap.add_argument("-n", type=int, default=3, help="samples per cell")
    ap.add_argument("--null", type=int, default=40, help="functions in the null group")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget", type=float, default=6.0)
    a = ap.parse_args()

    loc = FunctionLocator(ROOT / "subjects/vim_9_2_0015")
    ci = CalleeIndex(loc, json.loads((OUT / "codebase.json").read_text()))
    llm = LLM(cache_dir=None, budget_usd=a.budget)          # uncached: every sample is fresh

    z3 = json.loads((OUT / a.run / "z3_results.json").read_text())
    by: dict[str, list] = {}
    for d in z3:
        if d.get("feasible") and d.get("function"):
            by.setdefault(d["function"], []).append(R(d))

    chain_jobs, null_jobs = [], []
    for fn, items in by.items():
        func = loc.at(items[0].warning.file, items[0].warning.line)
        if func is None:
            continue
        cur = ci.context(func, items, 50, 1000)
        ch = chain_context(loc, func, items)
        (chain_jobs if ch else null_jobs).append((fn, func, items, cur, ch))
    random.Random(20260911).shuffle(null_jobs)
    null_jobs = null_jobs[: a.null]
    print(f"{a.run}: {len(chain_jobs)} functions with a trace chain, "
          f"null group {len(null_jobs)} of {len(by) - len(chain_jobs)} without one\n")

    def vote(prompt: str, tag: str) -> list[bool]:
        return [bool(parse_json(llm.chat(SYSTEM, prompt, tag=f"{tag}-{i}")).get("verdict"))
                for i in range(a.n)]

    def do_chain(job):
        fn, func, items, cur, ch = job
        pc = build_prompt("vim_9_2_0015", func, items, cur)
        ph = build_prompt("vim_9_2_0015", func, items, ch)
        vc, vh = vote(pc, f"ab-cur-{fn}"), vote(ph, f"ab-chn-{fn}")
        return {"function": fn, "file": func.file, "kind": "chain",
                "cur_votes": vc, "chn_votes": vh,
                "cur": majority(vc), "chn": majority(vh),
                "cur_chars": len(pc), "chn_chars": len(ph)}

    def do_null(job):
        fn, func, items, cur, _ = job
        p = build_prompt("vim_9_2_0015", func, items, cur)
        a1, a2 = vote(p, f"null-a-{fn}"), vote(p, f"null-b-{fn}")
        return {"function": fn, "file": func.file, "kind": "null",
                "a_votes": a1, "b_votes": a2, "cur": majority(a1), "chn": majority(a2)}

    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(do_chain, j) for j in chain_jobs] + [ex.submit(do_null, j) for j in null_jobs]
        for k, f in enumerate(as_completed(futs), 1):
            try:
                rows.append(f.result())
            except Exception as e:
                print("  ERR", e)
            if k % 25 == 0:
                print(f"  {k}/{len(futs)} done, ${llm.usage.cost_usd:.2f}")

    ch = [r for r in rows if r["kind"] == "chain"]
    nu = [r for r in rows if r["kind"] == "null"]
    t2f = [r["function"] for r in ch if r["cur"] and not r["chn"]]
    f2t = [r["function"] for r in ch if not r["cur"] and r["chn"]]
    nflip = [r["function"] for r in nu if r["cur"] != r["chn"]]
    unst = lambda vs: len(set(vs)) > 1
    print(f"\n=== chain group ({len(ch)} functions)")
    print(f"    majority true : current {sum(r['cur'] for r in ch)} -> chain {sum(r['chn'] for r in ch)}")
    print(f"    true -> false : {len(t2f)}   false -> true : {len(f2t)}   flips {len(t2f)+len(f2t)}/{len(ch)}"
          f" = {100*(len(t2f)+len(f2t))/max(len(ch),1):.0f}%")
    print(f"    non-unanimous : current {sum(unst(r['cur_votes']) for r in ch)}/{len(ch)}"
          f"   chain {sum(unst(r['chn_votes']) for r in ch)}/{len(ch)}")
    cc = [r["cur_chars"] for r in ch]; hh = [r["chn_chars"] for r in ch]
    print(f"    prompt chars  : current median {sorted(cc)[len(cc)//2]}  chain median {sorted(hh)[len(hh)//2]}")
    print(f"\n=== null group ({len(nu)} functions, identical prompt twice)")
    print(f"    majority flips: {len(nflip)}/{len(nu)} = {100*len(nflip)/max(len(nu),1):.0f}%  <- the floor")
    print(f"\n  true->false: {sorted(t2f)}")
    print(f"  false->true: {sorted(f2t)}")
    print(f"  null flips : {sorted(nflip)}")
    (ROOT / "results/llm-attribution/chain_ab_results.json").write_text(
        json.dumps({"run": a.run, "n": a.n, "rows": rows, "t2f": t2f, "f2t": f2t,
                    "null_flips": nflip, "usage": llm.usage.to_dict()}, indent=1))
    print("\nusage:", llm.usage.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
