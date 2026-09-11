#!/usr/bin/env python3
"""Does a second hop of callee bodies remove the reference-count false positives?

Diagnosis behind this: 15% of stage-3's confirmed functions rest on the claim that the function
"never releases its own reference" after `dict_add_list`. The model HAD `dict_add_list` (which
shows `++list->lv_refcount`) and usually `list_alloc` too, but not `list_init` -- called by
list_alloc, one hop further out -- so it could not confirm a fresh list starts at refcount 0 and
inferred that some decrement must be owed. The gap is mechanical, not a wording problem.

A 6-function pilot flipped 3 to false and made the other 3 keep `true` for the *correct* reason
(the OOM path where `dictitem_alloc` fails before the increment). So all 6 dropped the false
premise. This measures the same change over every function stage 3 judged, because widening the
context is a GLOBAL change: the 24 -> 47 episode showed such a change moves results everywhere,
and fixing 8 functions is worthless if it breaks 40 others.

Two scopes, since the right range is the open design question:
    narrow : hop-2 helpers reachable from callees the WARNING names (allocation site / trace)
    wide   : hop-2 helpers reachable from any callee already sent
both capped at <=25 lines per helper and <=8 helpers. Those caps are arbitrary; prompt growth is
reported so the cost of each is visible.

The hop-1 arm reuses the verdicts from the run under test -- a fresh sample from the same day.
Read every flip count against noise_floor.py: ~10% of single samples disagree with themselves.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memhint.llm import LLM, parse_json
from memhint.models import Warning
from memhint.verify import SYSTEM, CalleeIndex, FunctionLocator, build_prompt, _CALL, _KEYWORDS

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output/vim_9_2_0015"
MAX_HELPER_LINES, MAX_HELPERS = 25, 8
# 'neighbor' scope deliberately relaxes both: it goes deep on three call sites instead of wide on all
NB_HELPER_LINES, NB_HELPERS = 80, 16
SIG = re.compile(r"^([A-Za-z_]\w*)\s*\(", re.M)


class R:
    def __init__(self, d):
        self.warning = Warning.from_dict(d["warning"])
        self.path_lines = d.get("path_lines", [])


def named_callees(items) -> set[str]:
    """The callees the warning itself points at -- allocation site and message."""
    out: set[str] = set()
    for r in items:
        parts = (r.warning.allocation_site or "").split(":")
        if len(parts) == 3 and parts[2]:
            out.add(parts[2])
        out |= set(_CALL.findall(r.warning.message or ""))
    return out


def neighbor_callees(func, items) -> set[str]:
    """The callee the analyzer names, plus the call immediately before and after it in the body.

    The analyzer points at one call site; its attribution is often a line or two off, so the two
    neighbouring calls are the cheapest way to cover that without widening to the whole function.
    """
    order: list[str] = []
    for m in _CALL.finditer(func.code):                  # call sites in source order
        n = m.group(1)
        if n in _KEYWORDS or n == func.name:
            continue
        order.append(n)
    named = named_callees(items)
    out: set[str] = set()
    for i, n in enumerate(order):
        if n not in named:
            continue
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(order):
                out.add(order[j])
    return out or named


def hop2(ci, func, ctx: str, restrict: set[str] | None,
         max_lines: int = MAX_HELPER_LINES, max_helpers: int = MAX_HELPERS) -> tuple[str, list[str]]:
    """Bodies of short helpers called by the callees already in `ctx`.

    restrict=None -> wide (scan the whole context); otherwise only the blocks defining those names.
    """
    blocks = ctx.split("// src/")[1:]
    if restrict is not None:
        blocks = [b for b in blocks if (SIG.search(b) or [None]) and SIG.search(b)
                  and SIG.search(b).group(1) in restrict]
    have = {m.group(1) for b in ctx.split("// src/")[1:] if (m := SIG.search(b))}
    extra, names = [], []
    for n in dict.fromkeys(x for b in blocks for x in _CALL.findall(b)):
        if n in have or n in _KEYWORDS or n == func.name or len(extra) >= max_helpers:
            continue
        hit = ci.resolve(n, func.file)
        if hit is None:
            continue
        f, code = hit
        if len(code.splitlines()) <= max_lines:
            extra.append(f"// {f}\n{code}")
            names.append(n)
    if not extra:
        return "", []
    return ("\n**Bodies of short helpers those callees call:**\n```c\n"
            + "\n".join(extra) + "\n```\n"), names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="infer-v13argn")
    ap.add_argument("--scopes", default="narrow,wide")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--budget", type=float, default=5.0)
    a = ap.parse_args()

    loc = FunctionLocator(ROOT / "subjects/vim_9_2_0015")
    ci = CalleeIndex(loc, json.loads((OUT / "codebase.json").read_text()))
    llm = LLM(cache_dir=ROOT / "results/llm-attribution/cache", budget_usd=a.budget)
    z3 = json.loads((OUT / a.run / "z3_results.json").read_text())
    hop1 = {v["function"]: v for v in json.loads((OUT / a.run / "llm_verdicts.json").read_text())}

    by_fn: dict[str, list] = {}
    for d in z3:
        if d.get("feasible") and d.get("function"):
            by_fn.setdefault(d["function"], []).append(R(d))

    jobs = []
    for fn, items in by_fn.items():
        if fn not in hop1:
            continue
        func = loc.at(items[0].warning.file, items[0].warning.line)
        if func is None:
            continue
        ctx = ci.context(func, items, 50, 1000)
        jobs.append((fn, func, items, ctx))
    print(f"{len(jobs)} functions from {a.run}")

    res = {"run": a.run, "scopes": {}}
    for scope in a.scopes.split(","):
        def one(job):
            fn, func, items, ctx = job
            if scope == "wide":
                rst, lim = None, (MAX_HELPER_LINES, MAX_HELPERS)
            elif scope == "neighbor":
                rst, lim = neighbor_callees(func, items), (NB_HELPER_LINES, NB_HELPERS)
            else:
                rst, lim = named_callees(items), (MAX_HELPER_LINES, MAX_HELPERS)
            ex, names = hop2(ci, func, ctx, rst, *lim)
            if not ex:
                return fn, None, 0, names
            p = build_prompt("vim_9_2_0015", func, items, ctx + ex)
            d = parse_json(llm.chat(SYSTEM, p, tag=f"hop2-{scope}-{fn}"))
            return fn, d, len(ex), names

        rows, n_ctx = [], 0
        with ThreadPoolExecutor(max_workers=a.workers) as ex_:
            futs = [ex_.submit(one, j) for j in jobs]
            for f in as_completed(futs):
                try:
                    fn, d, grow, names = f.result()
                except Exception as e:
                    print("  ERR", e); continue
                was = bool(hop1[fn]["verdict"])
                if d is None:
                    rows.append({"function": fn, "was": was, "now": was, "no_helpers": True,
                                 "growth": 0, "helpers": []})
                    continue
                n_ctx += 1
                rows.append({"function": fn, "was": was, "now": bool(d.get("verdict")),
                             "conf_was": hop1[fn]["confidence"], "conf_now": d.get("confidence"),
                             "reason_was": hop1[fn]["reason"], "reason_now": d.get("reason"),
                             "growth": grow, "helpers": names})
        t2f = [r for r in rows if r["was"] and not r["now"]]
        f2t = [r for r in rows if not r["was"] and r["now"]]
        gr = [r["growth"] for r in rows if r["growth"]]
        print(f"\n=== scope={scope}: {n_ctx}/{len(rows)} functions actually got extra helpers")
        print(f"    prompt growth on those: median {sorted(gr)[len(gr)//2] if gr else 0} chars, max {max(gr) if gr else 0}")
        print(f"    true -> false : {len(t2f)}")
        print(f"    false -> true : {len(f2t)}")
        print(f"    net confirmed : {sum(r['was'] for r in rows)} -> {sum(r['now'] for r in rows)}")
        res["scopes"][scope] = {"rows": rows, "t2f": [r["function"] for r in t2f],
                                "f2t": [r["function"] for r in f2t]}
    res["usage"] = llm.usage.to_dict()
    (ROOT / "results/llm-attribution/hop2_results.json").write_text(json.dumps(res, indent=1))
    print("\nusage:", llm.usage.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
