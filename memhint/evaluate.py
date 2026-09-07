"""Ground truth from upstream: match our reported bugs against later leak-fix commits.

For each commit after the analysed tag whose subject mentions "leak", the diff
hunk headers (``@@ -a,b +c,d @@ <function context>``) give the (file, function)
pairs that were changed. A reported bug matches a fix when file and function
agree.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@\s*(.*)$")
_FUNC = re.compile(r"([A-Za-z_]\w*)\s*\(")


@dataclass
class Fix:
    sha: str
    subject: str
    funcs: set[tuple[str, str]] = field(default_factory=set)   # (file, function)
    files: set[str] = field(default_factory=set)

    @property
    def patch(self) -> str:
        m = re.search(r"patch (9\.\d+\.\d+)", self.subject)
        return m.group(1) if m else self.sha[:8]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True).stdout


def leak_fixes(repo: Path, base: str, head: str, pattern: str = "leak") -> list[Fix]:
    log = _git(repo, "log", f"{base}..{head}", "--format=%H%x09%s", "-i", f"--grep={pattern}")
    fixes: list[Fix] = []
    for line in log.splitlines():
        sha, subject = line.split("\t", 1)
        if re.search(r"^patch [\d.]+: tests?:", subject):
            continue
        fx = Fix(sha, subject)
        diff = _git(repo, "show", "--format=", "--unified=0", sha, "--", "*.c", "*.h")
        cur_file = None
        for l in diff.splitlines():
            if l.startswith("--- a/"):
                cur_file = l[6:]
            elif l.startswith("--- /dev/null"):
                cur_file = None
            m = _HUNK.match(l)
            if m and cur_file:
                fx.files.add(cur_file)
                ctx = m.group(3)
                # git's C hunk context is the enclosing definition line, e.g. "static void foo(int a)"
                fm = _FUNC.search(ctx)
                if fm:
                    fx.funcs.add((cur_file, fm.group(1)))
        fixes.append(fx)
    return fixes


def match(bugs: list[dict], fixes: list[Fix]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Return (fix.sha -> matching bugs, unmatched bugs)."""
    by_fix: dict[str, list[dict]] = {f.sha: [] for f in fixes}
    unmatched: list[dict] = []
    for b in bugs:
        hit = [f for f in fixes if (b["file"], b["function"]) in f.funcs]
        for f in hit:
            by_fix[f.sha].append(b)
        if not hit:
            unmatched.append(b)
    return by_fix, unmatched


def evaluate(repo: Path, out: Path, base: str, head: str, runs: dict[str, Path]) -> str:
    fixes = leak_fixes(repo, base, head)
    fixes_by_patch = sorted(fixes, key=lambda f: f.patch)
    bugs = {name: (json.loads(p.read_text()) if p.exists() else None) for name, p in runs.items()}
    matches = {name: match(b, fixes)[0] for name, b in bugs.items() if b is not None}
    unmatched = {name: match(b, fixes)[1] for name, b in bugs.items() if b is not None}

    md = [f"# Upstream ground truth: leak fixes in {base}..{head[:12]}", "",
          f"{len(fixes)} leak-related commits (test-only commits excluded).", "",
          "| patch | subject | changed functions | " + " | ".join(matches) + " |",
          "|---|---|---|" + "---|" * len(matches)]
    for f in fixes_by_patch:
        funcs = ", ".join(sorted({fn for _, fn in f.funcs})) or ", ".join(sorted(f.files))
        row = [f.patch, f.subject.split(": ", 1)[-1][:70], f"`{funcs[:80]}`"]
        for name in matches:
            row.append("✅" if matches[name][f.sha] else "")
        md.append("| " + " | ".join(row) + " |")
    md += ["", "## Summary", "", "| run | reported bugs | matching an upstream fix | distinct fixes hit | unmatched (FP or unfixed) |",
           "|---|---|---|---|---|"]
    result = {}
    for name, b in bugs.items():
        if b is None:
            continue
        hit_fixes = [s for s, v in matches[name].items() if v]
        n_matched = len(b) - len(unmatched[name])
        md.append(f"| {name} | {len(b)} | {n_matched} | {len(hit_fixes)} | {len(unmatched[name])} |")
        result[name] = {"reported": len(b), "matched": n_matched, "fixes_hit": len(hit_fixes),
                        "fixes_hit_patches": sorted(next(f.patch for f in fixes if f.sha == s) for s in hit_fixes),
                        "unmatched": [(x["file"], x["function"], x["line"]) for x in unmatched[name]]}
    (out / "ground_truth.json").write_text(json.dumps({
        "fixes": [{"sha": f.sha, "patch": f.patch, "subject": f.subject, "funcs": sorted(f.funcs)} for f in fixes_by_patch],
        "runs": result}, indent=1))
    text = "\n".join(md) + "\n"
    (out / "ground_truth.md").write_text(text)
    return text


if __name__ == "__main__":
    import sys
    repo, out = Path(sys.argv[1]), Path(sys.argv[2])
    runs = {d.name: d / "bugs.json" for d in sorted(out.iterdir()) if (d / "bugs.json").exists()}
    print(evaluate(repo, out, sys.argv[3], sys.argv[4], runs))
