"""Aggregate one project's outputs into a Markdown report next to the paper's numbers."""
from __future__ import annotations

import json
from pathlib import Path

PAPER = {  # Tables I, II, IV, V for the subjects we may run
    "vim_9_2_0015": {"name": "Vim", "extr": 11071, "cand": 8539, "summ": 2539, "valid": 688,
                     "codeql": {"warn": 1011, "z3": 86, "llm": 24, "bugs": 18},
                     "infer": {"warn": 1032, "z3": 147, "llm": 25, "bugs": 15},
                     "detected": 22, "vanilla_codeql": 10, "vanilla_infer": 3},
    "tmux_3_6_a": {"name": "tmux", "extr": 2612, "cand": 2528, "summ": 790, "valid": 467,
                   "codeql": {"warn": 291, "z3": 70, "llm": 13, "bugs": 9},
                   "infer": {"warn": 304, "z3": 36, "llm": 7, "bugs": 6},
                   "detected": 10, "vanilla_codeql": 7, "vanilla_infer": 0},
}


def _load(p: Path) -> dict | list | None:
    return json.loads(p.read_text()) if p.exists() else None


def _pct(a: int, b: int) -> str:
    return f"{100 * (1 - a / b):.1f}%" if b else "-"


def build(out: Path) -> str:
    proj = out.name
    paper = PAPER.get(proj, {})
    s1 = _load(out / "stage1_stats.json") or {}
    md = [f"# MemHint reproduction report — {paper.get('name', proj)}", ""]

    # Stage 1 funnel (Table IV)
    md += ["## Stage 1: summary generation (paper Table IV)", "",
           "| | #Extr. | #Cand. | #Summ. | #Valid. |", "|---|---|---|---|---|"]
    if s1:
        md.append(f"| ours | {s1.get('n_extracted')} | {s1.get('n_candidates')} ({_pct(s1.get('n_candidates', 0), s1.get('n_extracted', 0))} ↓) "
                  f"| {s1.get('n_summaries')} ({_pct(s1.get('n_summaries', 0), s1.get('n_candidates', 0))} ↓) "
                  f"| {s1.get('n_validated')} ({_pct(s1.get('n_validated', 0), s1.get('n_summaries', 0))} ↓) |")
    if paper:
        md.append(f"| paper | {paper['extr']} | {paper['cand']} ({_pct(paper['cand'], paper['extr'])} ↓) | {paper['summ']} ({_pct(paper['summ'], paper['cand'])} ↓) | {paper['valid']} ({_pct(paper['valid'], paper['summ'])} ↓) |")
    if s1.get("phase2_llm"):
        u = s1["phase2_llm"]
        md += ["", f"LLM (gpt-5.6-luna) summary generation: {u['calls']} calls, {u['prompt_tokens']:,} prompt + {u['completion_tokens']:,} completion tokens, **${u['cost_usd']:.2f}** "
               f"(paper, Gemini 3 Flash: ${ {'vim_9_2_0015': 10.20, 'tmux_3_6_a': 2.27}.get(proj, 0):.2f})"]

    # Stage 2/3 (Table V)
    md += ["", "## Stages 2-3: warnings → Z3 → LLM → bugs (paper Table V)", "",
           "| analyzer | | #Warn. | #Z3 | #LLM-valid. | #bugs reported |", "|---|---|---|---|---|---|"]
    for an in ("codeql", "infer"):
        for suffix in ("", "-vanilla"):
            d = out / (an + suffix)
            s2, s3 = _load(d / "stage2_stats.json") or {}, _load(d / "stage3_stats.json") or {}
            if not s2:
                continue
            w = s2.get("n_warnings", 0)
            z = s3.get("n_z3_feasible")
            l = s3.get("n_llm_confirmed_functions")
            b = s3.get("n_bugs")
            md.append(f"| {an}{suffix} | ours | {w} | {z if z is not None else '-'} ({_pct(z, w) if z is not None else '-'} ↓) "
                      f"| {l if l is not None else '-'} | {b if b is not None else '-'} |")
        if paper.get(an):
            p = paper[an]
            md.append(f"| {an} | paper | {p['warn']} | {p['z3']} ({_pct(p['z3'], p['warn'])} ↓) | {p['llm']} | {p['bugs']} confirmed |")
    if paper:
        md += ["", f"Paper baselines (Table II): vanilla CodeQL {paper['vanilla_codeql']}, vanilla Infer {paper['vanilla_infer']}; MemHint {paper['detected']} unique bugs."]

    # ground truth + manual review
    gt = _load(out / "ground_truth.json") or {}
    review = _load(out / "manual_review.json") or {}
    fixes = {f["sha"]: f for f in gt.get("fixes", [])}
    if gt:
        md += ["", "## Validation against upstream (see ground_truth.md)", "",
               "Upstream Vim commits after v9.2.0015 whose subject mentions *leak* are the oracle; a reported bug "
               "matches when (file, function) equals a changed function of such a commit. Unmatched bugs were reviewed "
               "manually (manual_review.json).", "",
               "| run | reported | matches upstream fix | distinct fixes hit | manual TP (unfixed upstream) | manual TP (fixed by a non-'leak' commit) | manual FP | not reviewed |",
               "|---|---|---|---|---|---|---|---|"]
        for name, r in gt.get("runs", {}).items():
            rv = review.get("verdicts", {})
            keys = {f"{f}:{fn}" for f, fn, _ in r["unmatched"]}
            tp = sum(1 for k in keys if rv.get(k, {}).get("verdict") == "TP")
            tpf = sum(1 for k in keys if rv.get(k, {}).get("verdict") == "TP-FIXED")
            fp = sum(1 for k in keys if rv.get(k, {}).get("verdict") == "FP")
            md.append(f"| {name} | {r['reported']} | {r['matched']} | {r['fixes_hit']} | {tp} | {tpf} | {fp} | {len(keys) - tp - tpf - fp} |")

    # bug list
    for an in ("codeql", "infer"):
        bugs = _load(out / an / "bugs.json")
        if bugs:
            rv = review.get("verdicts", {})
            matched_funcs = {}
            for f in gt.get("fixes", []):
                for file, fn in f["funcs"]:
                    matched_funcs.setdefault((file, fn), []).append(f["patch"])
            md += ["", f"## Reported bugs — {an} ({len(bugs)})", "",
                   "| file | function | line | rule | LLM reason | validation |", "|---|---|---|---|---|---|"]
            for b in sorted(bugs, key=lambda x: (x["file"], x["line"])):
                key = (b["file"], b["function"])
                if key in matched_funcs:
                    val = "fixed upstream: " + ", ".join(sorted(set(matched_funcs[key]))[:3])
                else:
                    v = rv.get(f"{b['file']}:{b['function']}", {})
                    val = f"manual {v['verdict']}: {v['note']}" if v else "unreviewed"
                md.append(f"| {b['file']} | `{b['function']}` | {b['line']} | {b['rule'].replace('cpp/', '')} | {b['llm_reason']} | {val} |")
    return "\n".join(md) + "\n"


if __name__ == "__main__":
    import sys
    print(build(Path(sys.argv[1])))
