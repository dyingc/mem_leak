"""MemHint command line.

  python -m memhint stage1 --project subjects/vim_9_2_0015 --out output/vim
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memhint")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("stage1", help="extract + LLM summaries + Z3 validation")
    s1.add_argument("--project", required=True, type=Path)
    s1.add_argument("--out", required=True, type=Path)
    s1.add_argument("--source-root", type=Path)
    s1.add_argument("--workers", type=int, default=8)
    s1.add_argument("--budget", type=float, default=20.0, help="max LLM spend in USD")
    s1.add_argument("--force", action="store_true", help="ignore checkpoints")
    s1.add_argument("--rules", type=Path,
                    help="file describing how this project manages memory (ownership conventions, "
                         "allocator families, reference counting or collection). Injected verbatim "
                         "after the generic instructions and before the function blocks; empty by "
                         "default, which leaves the prompt unchanged")
    s1.add_argument("--all-functions", action="store_true",
                    help="summarise every extracted non-macro function instead of the "
                         "pointer-signature pre-filter")

    for name, p in (("stage2", "run CodeQL/Infer with injected summaries"),
                    ("stage3", "Z3 feasibility filter + LLM validation")):
        s = sub.add_parser(name, help=p)
        s.add_argument("--project", required=True, type=Path)
        s.add_argument("--out", required=True, type=Path)
        s.add_argument("--analyzer", choices=["codeql", "infer"], required=True)
        s.add_argument("--vanilla", action="store_true", help="baseline: no summaries injected")
        s.add_argument("--threads", type=int, default=8)
        s.add_argument("--tag", help="suffix for the run directory: <analyzer>[-vanilla][-TAG]")
        s.add_argument("--hints", type=Path, help="use this hints.json instead of <out>/hints.json")
        if name == "stage2":
            s.add_argument("--tools", type=Path, default=Path("tools"))
            s.add_argument("--infer-report", type=Path, help="replay an existing Infer report.json instead of running Infer")
            s.add_argument("--infer-pattern-mode", choices=["anchored", "official", "anchored-argn"], default="anchored")
            s.add_argument("--infer-debug-level", type=int)
        else:
            s.add_argument("--workers", type=int, default=8)
            s.add_argument("--budget", type=float, default=20.0)
            s.add_argument("--skip-llm", action="store_true", help="only run the Z3 filter")
            s.add_argument("--max-callees", type=int, default=50,
                           help="callee bodies to put in the validation prompt (0 disables)")
            s.add_argument("--max-callee-lines", type=int, default=1000, help="lines kept per callee body")
            s.add_argument("--adjacent-findings", action="store_true",
                           help="on each REJECTED function, make a second LLM call asking whether some other "
                                "allocation in the shown code leaks; emitted with source=llm-adjacent")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd == "stage1":
        from .pipeline import Stage1
        Stage1(args.project, args.out, args.source_root, args.workers, args.budget,
               args.rules, args.all_functions).run(args.force)
    elif args.cmd == "stage2":
        from .pipeline import Stage2
        Stage2(args.project, args.out, args.analyzer, args.tools, args.vanilla, args.threads, args.tag, args.hints,
               args.infer_report, args.infer_pattern_mode, args.infer_debug_level).run()
    elif args.cmd == "stage3":
        from .pipeline import Stage3
        Stage3(args.project, args.out, args.analyzer, args.vanilla, args.workers, args.budget, args.skip_llm,
               args.tag, args.hints, args.max_callees, args.max_callee_lines,
               args.adjacent_findings).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
