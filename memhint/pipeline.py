"""Stage orchestration with on-disk checkpoints.

output/<project>/
  codebase.json      Phase 1  extracted functions/macros + pointer typedefs
  hints_raw.json     Phase 2  LLM summaries before validation
  hints.json         Phase 3  Z3-validated summaries (paper Appendix B format)
  stage1_stats.json  funnel numbers (Table IV columns)
  llm_cache/         cached LLM responses (re-runs are free)
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .analysis import SummaryValidator
from .extract import Codebase
from .llm import LLM
from .models import Summary
from .summarize import generate_summaries

log = logging.getLogger(__name__)


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1))


def hints_to_json(summaries: list[Summary]) -> dict:
    out: dict[str, list[dict]] = {}
    for s in summaries:
        out.setdefault(s.name, []).append(s.to_dict())
    return {"hints": out}


def hints_from_json(d: dict) -> list[Summary]:
    return [Summary.from_dict(h) for hs in d["hints"].values() for h in hs]


class Stage1:
    def __init__(self, project: Path, out: Path, source_root: Path | None = None,
                 workers: int = 8, budget_usd: float | None = 20.0):
        self.project = project
        self.out = out
        self.source_root = source_root
        self.workers = workers
        out.mkdir(parents=True, exist_ok=True)
        self.llm = LLM(cache_dir=out / "llm_cache", budget_usd=budget_usd)
        self.stats: dict = {}

    # Phase 1 ------------------------------------------------------------
    def extract(self, force: bool = False) -> Codebase:
        p = self.out / "codebase.json"
        if p.exists() and not force:
            cb = Codebase.from_dict(json.loads(p.read_text()))
            log.info("Phase 1: loaded %d functions from %s", len(cb.functions), p)
        else:
            t0 = time.time()
            cb = Codebase.extract(self.project, self.source_root)
            _dump(p, cb.to_dict())
            log.info("Phase 1: %d functions in %.0fs", len(cb.functions), time.time() - t0)
        cands = cb.candidates()
        self.stats.update(n_extracted=len(cb.functions), n_macros=sum(f.is_macro for f in cb.functions.values()),
                          n_candidates=len(cands), n_files=cb.n_files)
        return cb

    # Phase 2 ------------------------------------------------------------
    def summarize(self, cb: Codebase, force: bool = False) -> list[Summary]:
        p = self.out / "hints_raw.json"
        if p.exists() and not force:
            raw = hints_from_json(json.loads(p.read_text()))
            log.info("Phase 2: loaded %d raw hints from %s", len(raw), p)
        else:
            t0 = time.time()
            r = generate_summaries(cb, self.llm, workers=self.workers)
            raw = r.summaries
            _dump(p, hints_to_json(raw))
            self.stats.update(phase2_batches=r.n_batches, phase2_failed=r.n_failed,
                              phase2_seconds=round(time.time() - t0), phase2_llm=self.llm.usage.to_dict())
            log.info("Phase 2: %d hints in %.0fs, %s", len(raw), time.time() - t0, self.llm.usage.to_dict())
        self.stats.update(n_summaries=len(raw), n_summary_functions=len({s.name for s in raw}))
        return raw

    # Phase 3 ------------------------------------------------------------
    def validate(self, cb: Codebase, raw: list[Summary], force: bool = False) -> list[Summary]:
        p = self.out / "hints.json"
        if p.exists() and not force:
            valid = hints_from_json(json.loads(p.read_text()))
            log.info("Phase 3: loaded %d validated hints from %s", len(valid), p)
        else:
            t0 = time.time()
            v = SummaryValidator(cb.functions, raw)
            valid = v.validate_all(raw)
            _dump(p, hints_to_json(valid))
            rejected = [dict(s.to_dict(), reason=v.reasons.get((s.name, s.role.value, s.arg_index), ""))
                        for s in raw if s not in set(valid)]
            _dump(self.out / "hints_rejected.json", rejected)
            self.stats.update(phase3_seconds=round(time.time() - t0))
            log.info("Phase 3: %d/%d validated in %.0fs", len(valid), len(raw), time.time() - t0)
        self.stats.update(n_validated=len(valid), n_validated_functions=len({s.name for s in valid}))
        return valid

    def run(self, force: bool = False) -> list[Summary]:
        cb = self.extract(force)
        raw = self.summarize(cb, force)
        valid = self.validate(cb, raw, force)
        _dump(self.out / "stage1_stats.json", self.stats)
        log.info("Stage 1 funnel: extracted=%d -> candidates=%d -> summaries=%d -> validated=%d",
                 self.stats["n_extracted"], self.stats["n_candidates"], self.stats["n_summaries"], self.stats["n_validated"])
        return valid


def load_hints(out: Path, vanilla: bool = False) -> list[Summary]:
    if vanilla:
        return []
    return hints_from_json(json.loads((out / "hints.json").read_text()))


class Stage2:
    """Run one analyzer with (or, for the baseline, without) the validated summaries.

    output/<project>/<analyzer>[-vanilla]/
      results.sarif | report.json   raw analyzer output
      warnings.json                 normalised warnings (Stage 3 input)
    """

    def __init__(self, project: Path, out: Path, analyzer: str, tools: Path, vanilla: bool = False, threads: int = 8):
        self.project, self.out, self.analyzer, self.vanilla, self.threads = project, out, analyzer, vanilla, threads
        self.tools = tools
        self.dir = out / (analyzer + ("-vanilla" if vanilla else ""))
        self.dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> list:
        from .models import Warning
        summaries = load_hints(self.out, self.vanilla)
        t0 = time.time()
        if self.analyzer == "codeql":
            from .analyzers.codeql import CodeQL, write_model_pack, parse_sarif
            cq = CodeQL(self.tools / "codeql" / "codeql", self.threads)
            pack = write_model_pack(summaries, self.out / "codeql-ext" / "memhint-models") if summaries else None
            sarif = cq.analyze(self.out / "codeql-db", self.dir / "results.sarif", pack)
            warnings = parse_sarif(sarif)
        elif self.analyzer == "infer":
            from .analyzers.infer import Infer, parse_report
            inf = Infer(self.tools / "infer" / "bin" / "infer", self.threads)
            report = inf.analyze(self.out / "infer-out", summaries)
            (self.dir / "report.json").write_text(report.read_text())
            warnings = parse_report(self.dir / "report.json")
        else:
            raise ValueError(self.analyzer)
        _dump(self.dir / "warnings.json", [w.to_dict() for w in warnings])
        _dump(self.dir / "stage2_stats.json", {"n_warnings": len(warnings), "seconds": round(time.time() - t0),
                                               "n_summaries_injected": len(summaries)})
        log.info("Stage 2 [%s%s]: %d warnings in %.0fs", self.analyzer, "-vanilla" if self.vanilla else "",
                 len(warnings), time.time() - t0)
        return warnings


class Stage3:
    """Z3 feasibility filter + LLM validation on one analyzer's warnings.

    output/<project>/<analyzer>[-vanilla]/
      z3_results.json      every warning with feasible flag + reason
      llm_verdicts.json    per-function LLM verdicts
      bugs.json            final reported bugs
      stage3_stats.json    Table V columns
    """

    def __init__(self, project: Path, out: Path, analyzer: str, vanilla: bool = False,
                 workers: int = 8, budget_usd: float | None = 20.0, skip_llm: bool = False):
        self.project, self.out, self.analyzer, self.vanilla = project, out, analyzer, vanilla
        self.dir = out / (analyzer + ("-vanilla" if vanilla else ""))
        self.workers, self.skip_llm = workers, skip_llm
        self.llm = None if skip_llm else LLM(cache_dir=out / "llm_cache", budget_usd=budget_usd)

    def run(self) -> list:
        from .models import Warning
        from .verify import FunctionLocator, z3_filter, llm_verify
        warnings = [Warning.from_dict(d) for d in json.loads((self.dir / "warnings.json").read_text())]
        summaries = load_hints(self.out, self.vanilla)
        loc = FunctionLocator(self.project)
        t0 = time.time()
        z3r = z3_filter(warnings, loc, summaries)
        _dump(self.dir / "z3_results.json", [r.to_dict() for r in z3r])
        stats = {"n_warnings": len(warnings), "n_z3_feasible": sum(r.feasible for r in z3r),
                 "z3_seconds": round(time.time() - t0)}
        bugs = []
        if not self.skip_llm:
            verdicts = llm_verify(self.project.name, z3r, loc, self.llm, self.workers)
            _dump(self.dir / "llm_verdicts.json", [v.to_dict() for v in verdicts])
            for v in verdicts:
                if v.verdict:
                    items = [v.items[i - 1] for i in v.bug_indices if 0 < i <= len(v.items)] or v.items
                    for r in items:
                        bugs.append({"file": v.file, "function": v.function, "line": r.warning.line,
                                     "analyzer": self.analyzer, "rule": r.warning.rule, "message": r.warning.message,
                                     "llm_reason": v.reason, "confidence": v.confidence, "z3_path_lines": r.path_lines})
            _dump(self.dir / "bugs.json", bugs)
            stats.update(n_llm_functions=len(verdicts), n_llm_confirmed_functions=sum(v.verdict for v in verdicts),
                         n_bugs=len(bugs), llm=self.llm.usage.to_dict())
        _dump(self.dir / "stage3_stats.json", stats)
        log.info("Stage 3 [%s]: %s", self.analyzer, stats)
        return bugs
