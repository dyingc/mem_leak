#!/usr/bin/env python3
"""Check whether an Infer report (or a MemHint bugs.json) contains the leaks we already know about.

Two reference sets:

  PR_TARGETS   the 9 leaks we submitted upstream as results/upstream-prs/tip-9.2.1054/*.diff.
               These are ground truth: all 9 were reproduced on unpatched Vim (see
               results/upstream-prs/CHALLENGE_RESPONSE.md), so a scan that misses one has a
               false negative, not a disputed finding.

  CODEIUM      the job/channel lifetime code behind the real 11.9 GB leak observed on the user's
               own machine (notes/vim-codeium-heartbeat-leak.md). There is no single "correct"
               procedure here -- the leak is ~887K finished job_T objects never freed -- so we
               report anything landing in job.c/channel.c and let a human judge.

Usage:  check_known_leaks.py <report.json|bugs.json> [more.json ...]
"""
import json, sys, collections, pathlib

PR_TARGETS = [
    # (file,               procedure,                      what leaks)
    ("match.c",            "f_setmatches",                 "PR1 setmatches list refcount"),
    ("viminfo.c",      "barline_parse",                "PR2 viminfo continuation buf"),
    ("strings.c",      "string_reduce",                "PR3 stale current_funccal (UAR)"),
    ("vim9class.c",    "ex_class",                     "PR4 class_T on name alloc failure"),
    ("vim9generics.c", "parse_generic_func_type_args", "PR5 generic type args"),
    ("json.c",         "json_encode_lsp_msg",          "PR6 lsp msg buffer"),
    ("ex_docmd.c",     "ex_redir",                     "PR7 do_browse() NULL path"),
    ("gui_gtk_x11.c",  "gui_gtk_draw_string",          "PR8 gtk draw string"),
    ("if_xcmdsrv.c",   "serverRegisterName",           "PR9 clientserver name"),
]
CODEIUM_FILES = ("job.c", "channel.c")


def norm(f):
    """Reports carry the file either bare ("beval.c"), repo-relative ("src/beval.c") or
    absolute; compare on the tail so all three forms match."""
    f = (f or "").replace("\\", "/")
    return f.split("/")[-1]


def load(path):
    d = json.loads(pathlib.Path(path).read_text())
    out = []
    for b in d:
        # Infer report.json and MemHint warnings.json/bugs.json use different key names
        f = b.get("file") or b.get("path") or ""
        p = b.get("procedure") or b.get("function") or b.get("proc") or ""
        t = b.get("bug_type") or b.get("kind") or b.get("rule") or ""
        out.append({"file": norm(f), "proc": p, "type": t, "line": b.get("line")})
    return out


def report(path):
    rows = load(path)
    procs = collections.defaultdict(set)      # (file, proc) -> {types}
    for r in rows:
        procs[(r["file"], r["proc"])].add(r["type"])
    print(f"\n=== {path}  ({len(rows)} findings, {len(procs)} distinct file+procedure)")
    hit = 0
    print("  --- the 9 upstream PRs ---")
    for f, p, what in PR_TARGETS:
        types = procs.get((f, p))
        # also accept a finding anywhere in the same file, flagged separately
        same_file = sorted({pr for (ff, pr) in procs if ff == f})
        if types:
            hit += 1
            print(f"   HIT  {f:22s} {p:30s} {sorted(types)}   [{what}]")
        elif same_file:
            print(f"   miss {f:22s} {p:30s} -- but the file has: {same_file[:4]}{'...' if len(same_file)>4 else ''}")
        else:
            print(f"   MISS {f:22s} {p:30s} (nothing in this file)  [{what}]")
    print(f"  => {hit}/9 exact procedure hits")
    print("  --- job/channel (the 11.9 GB Codeium leak) ---")
    jc = [(f, p, sorted(t)) for (f, p), t in sorted(procs.items()) if f in CODEIUM_FILES]
    if jc:
        for f, p, t in jc:
            print(f"   {f:18s} {p:34s} {t}")
    else:
        print("   nothing in job.c or channel.c")
    return hit


if __name__ == "__main__":
    for a in sys.argv[1:]:
        try:
            report(a)
        except FileNotFoundError:
            print(f"\n=== {a}: MISSING")
