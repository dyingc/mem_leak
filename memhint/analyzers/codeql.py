"""Phase 4 - CodeQL: inject validated summaries as a data-extension model pack
(paper Appendix B) and run the memory-leak queries.

  allocationFunctionModel(namespace, type, subtypes, name, sizeArg, sizeMult, reallocArg, requiresDealloc)
  deallocationFunctionModel(namespace, type, subtypes, name, freedArg)
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

import yaml

from ..models import Role, Summary, Warning

log = logging.getLogger(__name__)

QUERIES = ["codeql/cpp-queries:Critical/MemoryNeverFreed.ql",
           "codeql/cpp-queries:Critical/MemoryMayNotBeFreed.ql"]


def write_model_pack(summaries: list[Summary], pack_dir: Path, pack_name: str = "memhint/models") -> Path:
    """Create a CodeQL model pack (qlpack.yml + models/memhint.model.yml)."""
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "models").mkdir(exist_ok=True)
    allocs = sorted({s.name for s in summaries if s.role is Role.ALLOCATOR})
    frees = sorted({(s.name, s.arg_index) for s in summaries if s.role is Role.DEALLOCATOR})
    ext = {"extensions": [
        {"addsTo": {"pack": "codeql/cpp-all", "extensible": "allocationFunctionModel"},
         "data": [["", "", False, n, "", "", "", True] for n in allocs]},
        {"addsTo": {"pack": "codeql/cpp-all", "extensible": "deallocationFunctionModel"},
         "data": [["", "", False, n, str(i)] for n, i in frees]},
    ]}
    (pack_dir / "models" / "memhint.model.yml").write_text(yaml.safe_dump(ext, sort_keys=False))
    (pack_dir / "qlpack.yml").write_text(yaml.safe_dump({
        "name": pack_name, "version": "0.0.1", "library": True,
        "extensionTargets": {"codeql/cpp-all": "*"},
        "dataExtensions": ["models/*.model.yml"],
    }, sort_keys=False))
    log.info("model pack %s: %d allocators, %d deallocators", pack_dir, len(allocs), len(frees))
    return pack_dir


class CodeQL:
    def __init__(self, codeql_bin: Path, threads: int = 8):
        self.bin = str(codeql_bin)
        self.threads = threads

    def run(self, args: list[str], **kw) -> subprocess.CompletedProcess:
        cmd = [self.bin] + args
        log.info("$ %s", " ".join(cmd))
        return subprocess.run(cmd, text=True, capture_output=True, **kw)

    def create_database(self, project: Path, db: Path, build_cmd: str) -> None:
        r = self.run(["database", "create", str(db), "--language=cpp", "--overwrite",
                      f"--command={build_cmd}", f"--threads={self.threads}"], cwd=project)
        if r.returncode:
            raise RuntimeError(f"codeql database create failed:\n{r.stderr[-3000:]}")

    def analyze(self, db: Path, sarif: Path, model_pack: Path | None = None,
                queries: list[str] = QUERIES) -> Path:
        args = ["database", "analyze", str(db), *queries, "--format=sarif-latest", f"--output={sarif}",
                f"--threads={self.threads}", "--rerun"]
        if model_pack is not None:
            name = yaml.safe_load((model_pack / "qlpack.yml").read_text())["name"]
            args += [f"--additional-packs={model_pack.parent}", f"--model-packs={name}"]
        t0 = time.time()
        r = self.run(args)
        if r.returncode:
            raise RuntimeError(f"codeql database analyze failed:\n{r.stderr[-3000:]}")
        log.info("codeql analyze done in %.0fs -> %s", time.time() - t0, sarif)
        return sarif


# --------------------------------------------------------------------------- #
# SARIF -> Warning
# --------------------------------------------------------------------------- #

def _loc(pl: dict) -> tuple[str, int, str]:
    phys = pl.get("physicalLocation", {})
    uri = phys.get("artifactLocation", {}).get("uri", "")
    line = phys.get("region", {}).get("startLine", 0)
    msg = pl.get("message", {}).get("text", "") if "message" in pl else ""
    return uri, line, msg


def parse_sarif(sarif: Path, analyzer: str = "codeql") -> list[Warning]:
    data = json.loads(sarif.read_text())
    out: list[Warning] = []
    for run in data.get("runs", []):
        rules = {r["id"]: r for r in run.get("tool", {}).get("driver", {}).get("rules", [])}
        for res in run.get("results", []):
            rule = res.get("ruleId", "")
            locs = res.get("locations", [])
            if not locs:
                continue
            uri, line, _ = _loc(locs[0])
            msg = res.get("message", {}).get("text", "")
            trace: list[dict] = []
            for rel in res.get("relatedLocations", []):
                u, l, m = _loc(rel)
                trace.append({"file": u, "line": l, "message": m})
            for cf in res.get("codeFlows", []):
                for tf in cf.get("threadFlows", []):
                    for step in tf.get("locations", []):
                        u, l, m = _loc(step.get("location", {}))
                        trace.append({"file": u, "line": l, "message": m})
            out.append(Warning(analyzer=analyzer, rule=rule, file=uri, line=line, function="",
                               message=msg, trace=trace,
                               extra={"rule_name": rules.get(rule, {}).get("name", "")}))
    log.info("parsed %d warnings from %s", len(out), sarif)
    return out
