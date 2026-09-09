"""Phase 4 - Infer/Pulse: validated summaries become regex patterns for
``--pulse-model-alloc-pattern`` / ``--pulse-model-free-pattern`` (paper Appendix B).

Infer compiles these patterns with OCaml's ``Str`` module and matches them with
``Str.string_match`` at position 0.  Consequences (verified on Infer 1.2.0):

* alternation is ``\\|`` and grouping is ``\\( \\)``; a POSIX/Python ``(a|b)`` is
  taken literally and matches nothing;
* the match is a *prefix* match, so an unanchored ``alloc`` also models
  ``alloc_does_fail``, ``alloc_cmdbuff`` ...;
* a matched function is replaced by the model even when its body is visible.

``mode="anchored"`` (default) wraps the names in ``^\\( ... \\)$`` so only exact
names are modelled.  Stock Infer can express only two of the four summary shapes --
an allocator whose *return value* owns the memory and a deallocator that frees its
*first* argument -- so that is all this mode injects.  ``mode="official"`` reproduces
the reference implementation (jiekeshi/MemHint ``adapters.py``): unanchored names,
every allocator and every deallocator regardless of target.

``mode="anchored-argn"`` needs our patched Infer (tools/infer-src, notes/infer-arg-models.patch)
and injects all four shapes:

===========================  =========================================
summary                      flag
===========================  =========================================
Allocator / return           ``--pulse-model-alloc-pattern``
Allocator / argN             ``--pulse-model-alloc-arg-pattern N:re``
Deallocator / arg0           ``--pulse-model-free-pattern``
Deallocator / argN (N >= 1)  ``--pulse-model-free-arg-pattern N:re``
===========================  =========================================

Infer gives a procedure at most one model and tries the release matchers first, so a
name that ends up in both an allocation and a release rule silently loses its
allocation model (and its callers then look like use-after-free).  ``analyze`` logs a
warning when that happens.
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

_STR_SPECIAL = set(r".^$*+?{}[]\|()")


def _str_escape(name: str) -> str:
    return "".join("\\" + c if c in _STR_SPECIAL else c for c in name)


def _anchored(names) -> str | None:
    return "^\\(" + "\\|".join(_str_escape(n) for n in sorted(names)) + "\\)$" if names else None


def patterns(summaries: list[Summary], mode: str = "anchored") -> tuple[str | None, str | None]:
    skip = {"main", "_main", ""}
    if mode == "official":
        allocs = sorted({s.name for s in summaries if s.role is Role.ALLOCATOR and s.name not in skip})
        frees = sorted({s.name for s in summaries if s.role is Role.DEALLOCATOR and s.name not in skip})
        mk = lambda names: "\\|".join(_str_escape(n) for n in names) if names else None
        return mk(allocs), mk(frees)
    # the return-value model would be wrong for an allocator that fills an out parameter
    allocs = {s.name for s in summaries
              if s.role is Role.ALLOCATOR and s.arg_index < 0 and s.name not in skip}
    frees = {s.name for s in summaries if s.role is Role.DEALLOCATOR and s.arg_index == 0}
    return _anchored(allocs), _anchored(frees)


def _by_position(summaries: list[Summary], role: Role, lo: int) -> list[str]:
    by_pos: dict[int, set[str]] = {}
    for s in summaries:
        if s.role is role and s.arg_index >= lo:
            by_pos.setdefault(s.arg_index, set()).add(s.name)
    return [f"{n}:{_anchored(names)}" for n, names in sorted(by_pos.items())]


def free_arg_patterns(summaries: list[Summary]) -> list[str]:
    """``N:regex`` values for ``--pulse-model-free-arg-pattern`` (our Infer patch): one entry per
    argument position N >= 1 that some validated deallocator releases.  Functions that release
    several arguments get one entry per position.  Position 0 goes through stock Infer's
    ``--pulse-model-free-pattern``."""
    return _by_position(summaries, Role.DEALLOCATOR, 1)


def alloc_arg_patterns(summaries: list[Summary]) -> list[str]:
    """``N:regex`` values for ``--pulse-model-alloc-arg-pattern`` (our Infer patch): allocators
    that hand the memory over through a ``T **out`` argument rather than the return value.
    Position 0 is included -- unlike deallocators, stock Infer cannot express any of these."""
    return _by_position(summaries, Role.ALLOCATOR, 0)


class Infer:
    def __init__(self, infer_bin: Path, jobs: int = 8, pattern_mode: str = "anchored", debug_level: int | None = None):
        self.bin = str(infer_bin)
        self.jobs = jobs
        self.pattern_mode = pattern_mode
        self.debug_level = debug_level

    def run(self, args: list[str], **kw) -> subprocess.CompletedProcess:
        cmd = [self.bin] + args
        log.info("$ %s", " ".join(a if len(a) < 120 else a[:117] + "..." for a in cmd))
        return subprocess.run(cmd, text=True, capture_output=True, **kw)

    def capture(self, project: Path, results_dir: Path, build_cmd: list[str]) -> None:
        r = self.run(["capture", "--results-dir", str(results_dir), "--"] + build_cmd, cwd=project)
        if r.returncode:
            raise RuntimeError(f"infer capture failed:\n{r.stderr[-3000:]}")

    def analyze(self, results_dir: Path, summaries: list[Summary] | None = None) -> Path:
        # --keep-going as in the reference implementation; --debug-level is optional
        # (the reference uses 2, it changes only log volume and run time, see REPRODUCTION.md).
        args = ["analyze", "--results-dir", str(results_dir), "--keep-going", "--pulse-only", f"--jobs={self.jobs}"]
        if self.debug_level is not None:
            args += ["--debug-level", str(self.debug_level)]
        if summaries:
            ap, fp = patterns(summaries, self.pattern_mode)
            if ap:
                args += ["--pulse-model-alloc-pattern", ap]
            if fp:
                args += ["--pulse-model-free-pattern", fp]
            if self.pattern_mode == "anchored-argn":  # needs the patched Infer (tools/infer-src)
                for spec in free_arg_patterns(summaries):
                    args += ["--pulse-model-free-arg-pattern", spec]
                for spec in alloc_arg_patterns(summaries):
                    args += ["--pulse-model-alloc-arg-pattern", spec]
            both = ({s.name for s in summaries if s.role is Role.ALLOCATOR}
                    & {s.name for s in summaries if s.role is Role.DEALLOCATOR})
            if both:
                # Infer models a procedure once and matches release rules first, so these keep
                # only their release semantics (a realloc-like wrapper loses its allocation).
                log.warning("%d function(s) are both allocator and deallocator; Infer will model "
                            "only the release side: %s", len(both), ", ".join(sorted(both)[:10]))
        t0 = time.time()
        r = self.run(args)
        if r.returncode:
            raise RuntimeError(f"infer analyze failed:\n{r.stderr[-3000:]}")
        log.info("infer analyze done in %.0fs", time.time() - t0)
        return results_dir / "report.json"


# "Memory dynamically allocated by `realloc`, indirectly via call to `get_arglist()` on line 475 is not freed ..."
# "Memory dynamically allocated by call to `alloc()` on line 12 is not freed ..."
# "Memory dynamically allocated by `vim_strsave (custom malloc)` on line 668 is not freed ..."
_VIA_RE = re.compile(r"via call to `([A-Za-z_]\w*)\(\)` on line (\d+)")
_BY_RE = re.compile(r"allocated by (?:call to )?`([A-Za-z_]\w*)(?:\(\))?(?: \(custom malloc\))?`(?: on line (\d+))?")


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
