"""Phase 4 - Infer/Pulse: validated summaries become regex patterns for
``--pulse-model-alloc-pattern`` / ``--pulse-model-free-pattern`` (paper Appendix B).

Infer's free pattern always means "the first argument is released", so only
``arg0`` deallocators are injected.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path

from ..models import Role, Summary, Warning

log = logging.getLogger(__name__)


def patterns(summaries: list[Summary]) -> tuple[str | None, str | None]:
    allocs = sorted({s.name for s in summaries if s.role is Role.ALLOCATOR})
    frees = sorted({s.name for s in summaries if s.role is Role.DEALLOCATOR and s.arg_index == 0})
    mk = lambda names: "^(" + "|".join(re.escape(n) for n in names) + ")$" if names else None
    return mk(allocs), mk(frees)


class Infer:
    def __init__(self, infer_bin: Path, jobs: int = 8):
        self.bin = str(infer_bin)
        self.jobs = jobs

    def run(self, args: list[str], **kw) -> subprocess.CompletedProcess:
        cmd = [self.bin] + args
        log.info("$ %s", " ".join(a if len(a) < 120 else a[:117] + "..." for a in cmd))
        return subprocess.run(cmd, text=True, capture_output=True, **kw)

    def capture(self, project: Path, results_dir: Path, build_cmd: list[str]) -> None:
        r = self.run(["capture", "--results-dir", str(results_dir), "--"] + build_cmd, cwd=project)
        if r.returncode:
            raise RuntimeError(f"infer capture failed:\n{r.stderr[-3000:]}")

    def analyze(self, results_dir: Path, summaries: list[Summary] | None = None) -> Path:
        args = ["analyze", "--results-dir", str(results_dir), "--pulse-only", f"--jobs={self.jobs}"]
        if summaries:
            ap, fp = patterns(summaries)
            if ap:
                args += ["--pulse-model-alloc-pattern", ap]
            if fp:
                args += ["--pulse-model-free-pattern", fp]
        t0 = time.time()
        r = self.run(args)
        if r.returncode:
            raise RuntimeError(f"infer analyze failed:\n{r.stderr[-3000:]}")
        log.info("infer analyze done in %.0fs", time.time() - t0)
        return results_dir / "report.json"


# "Memory dynamically allocated by `realloc`, indirectly via call to `get_arglist()` on line 475 is not freed ..."
# "Memory dynamically allocated by call to `alloc()` on line 12 is not freed ..."
_VIA_RE = re.compile(r"via call to `([A-Za-z_]\w*)\(\)` on line (\d+)")
_BY_RE = re.compile(r"allocated by (?:call to )?`([A-Za-z_]\w*)(?:\(\))?`(?: on line (\d+))?")


def _allocation_site(r: dict) -> str:
    msg = r.get("qualifier", "")
    m = _VIA_RE.search(msg) or _BY_RE.search(msg)
    callee = m.group(1) if m else ""
    line = m.group(2) if m and m.lastindex and m.lastindex >= 2 and m.group(2) else None
    if line is None:
        steps = r.get("bug_trace", [])
        if steps and steps[0].get("filename") == r.get("file"):
            line = str(steps[0].get("line_number", ""))
    return f"{r.get('file')}:{line}:{callee}" if line and callee else ""


def parse_report(report: Path, analyzer: str = "infer") -> list[Warning]:
    out: list[Warning] = []
    for r in json.loads(report.read_text()):
        if "MEMORY_LEAK" not in r.get("bug_type", ""):
            continue
        msg = r.get("qualifier", "")
        alloc = _allocation_site(r)
        trace = [{"file": s.get("filename", ""), "line": s.get("line_number", 0), "message": s.get("description", "")}
                 for s in r.get("bug_trace", [])]
        out.append(Warning(analyzer=analyzer, rule=r["bug_type"], file=r.get("file", ""), line=r.get("line", 0),
                           function=r.get("procedure", ""), message=msg, allocation_site=alloc, trace=trace,
                           extra={"severity": r.get("severity"), "hash": r.get("hash")}))
    log.info("parsed %d leak warnings from %s", len(out), report)
    return out
