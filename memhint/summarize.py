"""Phase 2 - LLM summary generation (paper III-A, Appendix A-A).

Each candidate function is shown to the LLM with its signature, body and the
source of up to five direct callees. Following the paper we batch 20
functions per call (IRIS-style batching); the instructions are the paper's
prompt verbatim, the per-function block is repeated for each batch member.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .extract import Codebase
from .llm import LLM, parse_json
from .models import FunctionInfo, Role, Summary

log = logging.getLogger(__name__)

MAX_CALLEES = 5
MAX_FUNC_LINES = 400      # guard against pathological prompts
MAX_CALLEE_LINES = 80
BATCH_SIZE = 20

SYSTEM = ("You are a memory safety expert analyzing C/C++ code to identify function semantics "
          "that help static analyzers detect memory leaks.")

INSTRUCTIONS = """## Task

Analyze each function below and determine whether it is a memory allocator, deallocator, or neither.

## Semantic Categories

### Allocator

Function returns **newly allocated heap memory** that caller must eventually free.

**Positive indicators:**
- Calls malloc/calloc/realloc/aligned_alloc/new/new[] and returns the result
- Calls another known allocator (e.g., g_malloc, xmalloc, kmalloc) and returns result
- Returns result of a wrapper function that allocates

**Negative indicators (NOT an allocator):**
- Returns pointer to static/global buffer
- Returns pointer to struct field or array member
- Returns one of the input arguments
- Allocates internally but doesn't return the allocated memory
- Returns stack-allocated memory (dangling pointer bug, but not allocator semantic)

### Deallocator

Function **frees/releases memory** passed as an argument.

**Positive indicators:**
- Calls free/delete/delete[]/g_free/kfree on an argument
- Calls another deallocator on an argument
- Wrapper around resource cleanup

## Analysis Guidelines

1. **Trace data flow:** Follow where return values come from and where arguments flow to.
2. **Consider all paths:** Check all branches and return statements.
3. **Indirect calls matter:** If function calls helper that allocates/frees, propagate that semantic.
4. **Be precise:** Only report semantics you can verify from the code.

## Output Format

Return a JSON object with a `hints` array. Each hint is a function summary with:
- `name`: the function name
- `role`: "Allocator" or "Deallocator"
- `target`: "return" for allocators (return value carries heap ownership), or "argN" for deallocators (the N-th argument is freed, 0-indexed)

```json
{
    "hints": [
        {"name": "<func_name>", "role": "Allocator", "target": "return"},
        {"name": "<func_name>", "role": "Deallocator", "target": "arg0"}
    ]
}
```

A function may appear more than once (e.g. a realloc-like function is both a Deallocator of an argument and an Allocator of its return value). Functions with no memory semantics must not appear in `hints`. If none of the functions apply, return: `{"hints": []}`

Now analyze the functions below and return the JSON result.
"""


def _clip(code: str, n: int) -> str:
    lines = code.splitlines()
    if len(lines) <= n:
        return code
    return "\n".join(lines[:n]) + f"\n/* ... {len(lines) - n} more lines ... */"


def function_block(f: FunctionInfo, cb: Codebase, idx: int) -> str:
    params = ", ".join(f"{p.type} {p.name}".strip() for p in f.params) or "void"
    parts = [f"### Function {idx}: `{f.name}`",
             f"**Return type:** `{f.return_type or ('(macro)' if f.is_macro else '')}`",
             f"**Parameters:** `{params}`",
             "```c", _clip(f.code, MAX_FUNC_LINES), "```"]
    ctx = []
    for c in sorted(f.callees)[:MAX_CALLEES]:
        cf = cb.functions.get(c)
        if cf and cf.name != f.name:
            ctx.append(f"// Called function: {cf.name}\n{_clip(cf.code, MAX_CALLEE_LINES)}")
    if ctx:
        parts += ["Context (called functions):", "```c", "\n\n".join(ctx), "```"]
    return "\n".join(parts)


def build_prompt(batch: list[FunctionInfo], cb: Codebase) -> str:
    blocks = [function_block(f, cb, i + 1) for i, f in enumerate(batch)]
    return INSTRUCTIONS + "\n\n" + "\n\n".join(blocks)


def parse_hints(text: str, names: set[str]) -> list[Summary]:
    out: list[Summary] = []
    for h in parse_json(text).get("hints", []) or []:
        try:
            name, role, target = h["name"], Role(h["role"]), str(h["target"]).strip()
        except (KeyError, ValueError):
            log.debug("bad hint %r", h)
            continue
        if name not in names:
            continue
        if role is Role.ALLOCATOR and target != "return":
            continue
        if role is Role.DEALLOCATOR and not (target.startswith("arg") and target[3:].isdigit()):
            continue
        s = Summary(name, role, target)
        if s not in out:
            out.append(s)
    return out


@dataclass
class SummaryResult:
    summaries: list[Summary]
    n_batches: int
    n_failed: int


def generate_summaries(cb: Codebase, llm: LLM, candidates: list[FunctionInfo] | None = None,
                       batch_size: int = BATCH_SIZE, workers: int = 8) -> SummaryResult:
    cands = candidates if candidates is not None else cb.candidates()
    batches = [cands[i:i + batch_size] for i in range(0, len(cands), batch_size)]
    log.info("Phase 2: %d candidates in %d batches of %d", len(cands), len(batches), batch_size)

    results: list[Summary] = []
    failed = 0

    def run(i: int, batch: list[FunctionInfo]) -> list[Summary]:
        names = {f.name for f in batch}
        text = llm.chat(SYSTEM, build_prompt(batch, cb), tag=f"summ-{i}")
        try:
            return parse_hints(text, names)
        except ValueError as e:
            # one retry, unbatched parse failures are rare
            log.warning("batch %d unparsable (%s); retrying once", i, e)
            text = llm.chat(SYSTEM, build_prompt(batch, cb) + "\n\nReturn ONLY the JSON object.", tag=f"summ-{i}-retry")
            return parse_hints(text, names)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run, i, b): i for i, b in enumerate(batches)}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                results.extend(fut.result())
            except Exception as e:
                failed += 1
                log.error("batch %d failed: %s", futs[fut], e)
            if done % 25 == 0 or done == len(batches):
                log.info("  %d/%d batches, %d hints so far, $%.3f", done, len(batches),
                         len(results), llm.usage.cost_usd)
    return SummaryResult(summaries=sorted(set(results), key=lambda s: (s.name, s.role.value, s.target)),
                         n_batches=len(batches), n_failed=failed)
