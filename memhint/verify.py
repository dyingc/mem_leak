"""Stage 3 - Warning validation.

Phase 5: Z3 path feasibility (analysis.leak_feasible) on the flagged function.
Phase 6: LLM validation, one function per call, prompt from paper Appendix A-B.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .analysis import OwnershipModel, leak_feasible
from .extract import extract_file
from .llm import LLM, parse_json
from .models import FunctionInfo, Summary, Warning

log = logging.getLogger(__name__)

_KEYWORDS = {"if", "while", "for", "switch", "return", "sizeof", "do", "else", "case"}


# --------------------------------------------------------------------------- #
# locate the function containing a warning
# --------------------------------------------------------------------------- #

class FunctionLocator:
    def __init__(self, project: Path):
        self.project = project
        self._cache: dict[str, list[FunctionInfo]] = {}

    def functions_in(self, rel: str) -> list[FunctionInfo]:
        if rel not in self._cache:
            p = self.project / rel
            try:
                funcs, _ = extract_file(p, rel)
            except Exception as e:
                log.warning("cannot parse %s: %s", rel, e)
                funcs = []
            # tree-sitter error recovery can yield pseudo-functions named `if`/`while`
            self._cache[rel] = [f for f in funcs if not f.is_macro and f.name not in _KEYWORDS]
        return self._cache[rel]

    def at(self, rel: str, line: int) -> FunctionInfo | None:
        best = None
        for f in self.functions_in(rel):
            if f.start_line <= line <= f.end_line and (best is None or f.end_line - f.start_line < best.end_line - best.start_line):
                best = f
        return best


# --------------------------------------------------------------------------- #
# Phase 5
# --------------------------------------------------------------------------- #

@dataclass
class Z3Result:
    warning: Warning
    function: str
    feasible: bool
    reason: str
    path_lines: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"warning": self.warning.to_dict(), "function": self.function, "feasible": self.feasible,
                "reason": self.reason, "path_lines": self.path_lines}


def z3_filter(warnings: list[Warning], locator: FunctionLocator, summaries: list[Summary]) -> list[Z3Result]:
    model = OwnershipModel()
    for s in summaries:
        model.add(s)
    out: list[Z3Result] = []
    for w in warnings:
        f = locator.at(w.file, w.line)
        if f is None:
            out.append(Z3Result(w, "", True, "function not found; kept"))
            continue
        w.function = f.name
        alloc_line, alloc_callee = w.line, None
        if w.allocation_site:                       # Infer: "file:line:callee"
            parts = w.allocation_site.split(":")
            alloc_line, alloc_callee = int(parts[1]), parts[2]
        r = leak_feasible(f, model, alloc_line=alloc_line, alloc_callee=alloc_callee)
        out.append(Z3Result(w, f.name, r.feasible, r.reason, r.path_lines))
    kept = sum(r.feasible for r in out)
    log.info("Phase 5: %d/%d warnings feasible (%.1f%% filtered)", kept, len(out), 100 * (1 - kept / max(len(out), 1)))
    return out


# --------------------------------------------------------------------------- #
# Phase 6
# --------------------------------------------------------------------------- #

SYSTEM = ("Role: You are a senior static-analysis engineer specializing in C/C++ memory-safety. "
          "You review memory-leak bug reports and assess whether the reported findings correspond to "
          "actual memory-leak defects in the program.")


def build_prompt(project: str, func: FunctionInfo, items: list[Z3Result]) -> str:
    lines = func.code.splitlines()
    bug_lines = {r.warning.line - func.start_line for r in items}
    src = "\n".join(l + ("   // <-- reported bug" if i in bug_lines else "") for i, l in enumerate(lines))
    issues = []
    for k, r in enumerate(items, 1):
        w = r.warning
        rel = w.line - func.start_line
        code_at = lines[rel].strip() if 0 <= rel < len(lines) else ""
        block = [f"  {k}. Line {w.line}: {w.message}",
                 f"    allocation_site: {w.allocation_site or f'{w.file}:{w.line}'}"]
        for i, st in enumerate(w.trace[:12], 1):
            block.append(f"    trace step {i}: {st['file']}:{st['line']} {st['message']}".rstrip())
        block.append(f"    code at line {w.line}:\n      {code_at}")
        issues.append("\n".join(block))
    return f"""You are a memory-safety expert. Analyze the following C function and the reported bug(s).

**Project:** {project}
**File:** {func.file}
**Function:** {func.name}
**Reported category:** memory leak

**Reported issues (numbered 1, 2, ... for reference):**
{chr(10).join(issues)}

**Function source:**
```c
{src}
```

Does this function actually have a memory leak (heap memory that is allocated and, on some execution path, neither freed nor ownership-transferred)? Determine whether the reported issue is a genuine bug (true) or a false alarm (false).

Decision policy:
  - true: at least one reported issue plausibly corresponds to a real memory leak based on the shown code.
  - false: all reported issues are not real defects based on the shown code.
If only some issues are real, output true and list only the real ones by index (1 = first, 2 = second, ...).

Respond with a single JSON object, no other text. Keep reason SHORT:
  {{"verdict": true | false, "confidence": 0.0-1.0, "reason": "one short sentence", "bug_indices": [1] or [2,3] or []}}

Output rules:
  - bug_indices: 1-based indices of reported issues you consider real; [] when verdict=false.
  - reason: ONE short sentence only. Do not quote code.
"""


@dataclass
class LLMVerdict:
    function: str
    file: str
    verdict: bool
    confidence: float
    reason: str
    bug_indices: list[int]
    items: list[Z3Result]

    def to_dict(self) -> dict:
        return {"function": self.function, "file": self.file, "verdict": self.verdict, "confidence": self.confidence,
                "reason": self.reason, "bug_indices": self.bug_indices,
                "warnings": [r.to_dict() for r in self.items]}


def llm_verify(project: str, results: list[Z3Result], locator: FunctionLocator, llm: LLM,
               workers: int = 8) -> list[LLMVerdict]:
    groups: dict[tuple[str, str], list[Z3Result]] = {}
    for r in results:
        if r.feasible and r.function:
            groups.setdefault((r.warning.file, r.function), []).append(r)
    log.info("Phase 6: %d feasible warnings in %d functions", sum(len(v) for v in groups.values()), len(groups))

    def run(key: tuple[str, str], items: list[Z3Result]) -> LLMVerdict:
        func = locator.at(key[0], items[0].warning.line)
        text = llm.chat(SYSTEM, build_prompt(project, func, items), tag=f"verify-{func.name}")
        try:
            d = parse_json(text)
        except ValueError:
            text = llm.chat(SYSTEM, build_prompt(project, func, items) + "\nReturn ONLY the JSON object.", tag=f"verify-{func.name}-retry")
            d = parse_json(text)
        idx = [int(i) for i in d.get("bug_indices", []) or [] if str(i).isdigit()]
        return LLMVerdict(func.name, key[0], bool(d.get("verdict")), float(d.get("confidence", 0) or 0),
                          str(d.get("reason", "")), idx, items)

    out: list[LLMVerdict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run, k, v): k for k, v in groups.items()}
        for fut in as_completed(futs):
            try:
                out.append(fut.result())
            except Exception as e:
                log.error("verify %s failed: %s", futs[fut], e)
    n_true = sum(v.verdict for v in out)
    log.info("Phase 6: %d/%d functions confirmed by LLM, $%.3f", n_true, len(out), llm.usage.cost_usd)
    return sorted(out, key=lambda v: (v.file, v.function))
