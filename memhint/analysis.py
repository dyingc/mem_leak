"""Ownership analysis on the CFG: shared by Phase 3 (summary validation, paper
Eq. 1-2) and Phase 5 (warning feasibility, Eq. 3-5).

Tracked state for a pointer class C (a variable and its identifier aliases):
  alloc   set at calls to a known allocator whose result lands in C;
          cleared on the null branch of a check on C and when C is overwritten
  freed   set at calls to a known deallocator whose freed argument is in C
  escaped set when C is returned or stored into a non-local lvalue
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import z3

from .cfg import CFG, CFGBuilder, Call, Assign, Return, base_ident, is_plain_ident
from .models import FunctionInfo, Role, Summary
from .symbolic import PathEncoder, StateSpec

log = logging.getLogger(__name__)

STD_ALLOCATORS = {"malloc", "calloc", "realloc", "reallocarray", "strdup", "strndup", "wcsdup",
                  "aligned_alloc", "memalign", "valloc", "pvalloc", "posix_memalign",
                  "new", "operator new", "xmalloc", "xcalloc", "xrealloc", "xstrdup",
                  "g_malloc", "g_malloc0", "g_strdup", "kmalloc", "kzalloc", "kcalloc", "vmalloc", "kstrdup"}
STD_DEALLOCATORS: dict[str, set[int]] = {"free": {0}, "cfree": {0}, "delete": {0}, "operator delete": {0},
                                        "xfree": {0}, "g_free": {0}, "kfree": {0}, "kvfree": {0}, "vfree": {0}}

MAX_DEPTH = 10


class OwnershipModel:
    """Known allocators / deallocators (std primitives + validated summaries)."""

    def __init__(self) -> None:
        self.allocators: set[str] = set(STD_ALLOCATORS)
        self.deallocators: dict[str, set[int]] = {k: set(v) for k, v in STD_DEALLOCATORS.items()}

    def add(self, s: Summary) -> None:
        if s.role is Role.ALLOCATOR:
            self.allocators.add(s.name)
        else:
            self.deallocators.setdefault(s.name, set()).add(s.arg_index)

    def frees(self, name: str) -> set[int]:
        return self.deallocators.get(name, set())


# --------------------------------------------------------------------------- #
# aliases
# --------------------------------------------------------------------------- #

class Aliases:
    """Flow-insensitive identifier alias classes from `v = p` / `v = (T*)p` assignments."""

    def __init__(self, cfg: CFG):
        self.parent: dict[str, str] = {}
        for _, e in cfg.events():
            if isinstance(e, Assign) and is_plain_ident(e.lhs) and is_plain_ident(e.rhs) and e.lhs != e.rhs:
                self.union(e.lhs, e.rhs)

    def find(self, x: str) -> str:
        while self.parent.get(x, x) != x:
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def cls(self, v: str) -> set[str]:
        r = self.find(v)
        return {x for x in set(self.parent) | {v} if self.find(x) == r}


def _in_class(expr: str, C: set[str]) -> bool:
    """Is expression text (already whitespace-normalised) a member of the class, modulo casts/&/*?"""
    t = re.sub(r"^\((?:const|struct|unsigned|signed|\w|\s|\*)+\)", "", expr)  # strip a leading cast
    return is_plain_ident(t) and t in C


# --------------------------------------------------------------------------- #
# state construction
# --------------------------------------------------------------------------- #

@dataclass
class Tracked:
    C: set[str]
    alloc_nodes: set[int] = field(default_factory=set)
    clear_nodes: set[int] = field(default_factory=set)
    free_nodes: set[int] = field(default_factory=set)
    escape_nodes: set[int] = field(default_factory=set)
    return_nodes: set[int] = field(default_factory=set)   # nodes returning a member of C


def track(cfg: CFG, C: set[str], model: OwnershipModel, alloc_nodes: set[int] | None = None) -> Tracked:
    t = Tracked(C=C)
    for n in cfg:
        for e in n.events:
            if isinstance(e, Call):
                if e.result is not None and is_plain_ident(e.result) and e.result in C:
                    if e.name in model.allocators:
                        t.alloc_nodes.add(n.id)
                    else:
                        t.clear_nodes.add(n.id)        # overwritten by an unknown value
                for i in model.frees(e.name):
                    if i < len(e.args) and _in_class(e.args[i], C):
                        t.free_nodes.add(n.id)
            elif isinstance(e, Assign):
                if is_plain_ident(e.lhs) and e.lhs in C and not _in_class(e.rhs, C):
                    t.clear_nodes.add(n.id)            # p = NULL / p = other
                if _in_class(e.rhs, C) and not (is_plain_ident(e.lhs) and e.lhs in cfg.locals):
                    t.escape_nodes.add(n.id)           # stored into global / field / deref
            elif isinstance(e, Return):
                if e.expr is not None and _in_class(e.expr, C):
                    t.escape_nodes.add(n.id)
                    t.return_nodes.add(n.id)
        # null check on the tracked pointer: the "is NULL" edge means allocation failed
        if n.kind == "BRANCH" and n.cond_kind == "bool" and n.cond_key in C:
            for v, label in n.succ:
                if label == "F":
                    t.clear_nodes.add(v)
    if alloc_nodes is not None:
        t.alloc_nodes = set(alloc_nodes)
    # a node that both allocates and is a clear point keeps the allocation
    t.clear_nodes -= t.alloc_nodes
    return t


def encode(cfg: CFG, t: Tracked) -> tuple[PathEncoder, dict, dict, dict]:
    pe = PathEncoder(cfg)
    a = pe.add_state(StateSpec("alloc", t.alloc_nodes, t.clear_nodes))
    f = pe.add_state(StateSpec("freed", t.free_nodes))
    s = pe.add_state(StateSpec("esc", t.escape_nodes))
    return pe, a, f, s


# --------------------------------------------------------------------------- #
# Phase 3: summary validation
# --------------------------------------------------------------------------- #

class SummaryValidator:
    """Eq. (1)/(2) with transitive delegation through LLM-labelled wrappers (depth <= 10)."""

    def __init__(self, functions: dict[str, FunctionInfo], candidates: list[Summary]):
        self.functions = functions
        self.hinted_alloc = {s.name for s in candidates if s.role is Role.ALLOCATOR}
        self.hinted_free: dict[str, set[int]] = {}
        for s in candidates:
            if s.role is Role.DEALLOCATOR:
                self.hinted_free.setdefault(s.name, set()).add(s.arg_index)
        self.model = OwnershipModel()
        self.memo: dict[tuple[str, str, int], bool] = {}
        self.visiting: set[tuple[str, str, int]] = set()
        self._cfg_cache: dict[str, CFG | None] = {}
        self.reasons: dict[tuple[str, str, int], str] = {}

    # ---- helpers ---------------------------------------------------------
    def cfg(self, name: str) -> CFG | None:
        if name not in self._cfg_cache:
            f = self.functions.get(name)
            try:
                self._cfg_cache[name] = CFGBuilder.from_code(f.code, cpp=f.file.endswith((".cpp", ".cc", ".cxx", ".hpp"))) if f and not f.is_macro else None
            except Exception as e:  # pragma: no cover
                log.warning("cfg(%s): %s", name, e)
                self._cfg_cache[name] = None
        return self._cfg_cache[name]

    def _local_model(self, depth: int) -> OwnershipModel:
        """Ownership model where hinted-but-unvalidated callees are resolved lazily."""
        return self.model

    # ---- entry points ----------------------------------------------------
    def validate(self, s: Summary, depth: int = 0) -> bool:
        key = (s.name, s.role.value, s.arg_index)
        if key in self.memo:
            return self.memo[key]
        if key in self.visiting or depth > MAX_DEPTH:
            return False
        self.visiting.add(key)
        try:
            ok = self._is_allocator(s.name, depth) if s.role is Role.ALLOCATOR else self._is_deallocator(s.name, s.arg_index, depth)
        finally:
            self.visiting.discard(key)
        self.memo[key] = ok
        if ok:
            self.model.add(s)
        return ok

    def validate_all(self, candidates: list[Summary]) -> list[Summary]:
        return [s for s in candidates if self.validate(s)]

    # ---- transitive resolution ------------------------------------------
    def _callee_is_allocator(self, name: str, depth: int) -> bool:
        if name in self.model.allocators:
            return True
        if name in self.hinted_alloc and name in self.functions:
            return self.validate(Summary(name, Role.ALLOCATOR, "return"), depth + 1)
        return False

    def _callee_frees(self, name: str, depth: int) -> set[int]:
        out = set(self.model.frees(name))
        for i in self.hinted_free.get(name, ()):
            if i not in out and name in self.functions and \
                    self.validate(Summary(name, Role.DEALLOCATOR, f"arg{i}"), depth + 1):
                out.add(i)
        return out

    def _resolved_model(self, cfg: CFG, depth: int) -> OwnershipModel:
        """Resolve every callee of this function once, so `track` sees wrappers as primitives."""
        m = OwnershipModel()
        m.allocators |= self.model.allocators
        for k, v in self.model.deallocators.items():
            m.deallocators.setdefault(k, set()).update(v)
        for _, e in cfg.events():
            if isinstance(e, Call):
                if e.result is not None and self._callee_is_allocator(e.name, depth):
                    m.allocators.add(e.name)
                fr = self._callee_frees(e.name, depth)
                if fr:
                    m.deallocators.setdefault(e.name, set()).update(fr)
        return m

    # ---- Eq. (1) ---------------------------------------------------------
    def _is_allocator(self, name: str, depth: int) -> bool:
        f = self.functions.get(name)
        if f is None:
            return False
        if f.is_macro:
            return self._macro_is_allocator(f, depth)
        cfg = self.cfg(name)
        if cfg is None:
            return False
        model = self._resolved_model(cfg, depth)
        al = Aliases(cfg)
        # candidate result variables: every alloc-call result (incl. `return f()` -> __ret__)
        vars_ = {e.result for _, e in cfg.events()
                 if isinstance(e, Call) and e.result and e.name in model.allocators and is_plain_ident(e.result)}
        seen: set[str] = set()
        for v in vars_:
            C = al.cls(v)
            if frozenset(C) in seen:
                continue
            seen.add(frozenset(C))
            t = track(cfg, C, model)
            if not t.return_nodes:
                continue
            pe, a, fr, _ = encode(cfg, t)
            for r in t.return_nodes:
                if pe.sat(pe.reach[r], a[r], z3.Not(fr[r])):
                    self.reasons[(name, "Allocator", -1)] = f"alloc of {sorted(C)} reaches return at L{cfg.nodes[r].line}"
                    return True
        self.reasons[(name, "Allocator", -1)] = "no feasible path from an allocation to a return of it"
        return False

    # ---- Eq. (2) ---------------------------------------------------------
    def _is_deallocator(self, name: str, idx: int, depth: int) -> bool:
        f = self.functions.get(name)
        if f is None or idx < 0:
            return False
        if f.is_macro:
            return self._macro_is_deallocator(f, idx, depth)
        cfg = self.cfg(name)
        if cfg is None or idx >= len(cfg.params):
            return False
        model = self._resolved_model(cfg, depth)
        C = Aliases(cfg).cls(cfg.params[idx])
        t = track(cfg, C, model)
        if not t.free_nodes:
            self.reasons[(name, "Deallocator", idx)] = f"no call frees {cfg.params[idx]} (or an alias)"
            return False
        pe = PathEncoder(cfg)
        for n in t.free_nodes:
            if pe.sat(pe.reach[n]):
                self.reasons[(name, "Deallocator", idx)] = f"{cfg.params[idx]} freed at L{cfg.nodes[n].line}"
                return True
        self.reasons[(name, "Deallocator", idx)] = "free sites unreachable on any feasible path"
        return False

    # ---- macros ----------------------------------------------------------
    def _macro_body(self, f: FunctionInfo) -> str:
        code = f.code.replace("\\\n", " ")
        m = re.match(r"\s*#\s*define\s+" + re.escape(f.name) + r"(?:\([^)]*\))?", code)
        return code[m.end():] if m else code

    def _macro_is_allocator(self, f: FunctionInfo, depth: int) -> bool:
        body = self._macro_body(f)
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body):
            if m.group(1) != f.name and self._callee_is_allocator(m.group(1), depth):
                return True
        return False

    def _macro_is_deallocator(self, f: FunctionInfo, idx: int, depth: int) -> bool:
        params = [p.name for p in f.params]
        if idx >= len(params):
            return False
        p = params[idx]
        body = self._macro_body(f)
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", body):
            callee = m.group(1)
            if callee == f.name:
                continue
            fr = self._callee_frees(callee, depth)
            if not fr:
                continue
            args = [a.strip() for a in m.group(2).split(",")]
            for i in fr:
                if i < len(args) and re.fullmatch(r"\(?\s*" + re.escape(p) + r"\s*\)?", args[i]):
                    return True
        return False


# --------------------------------------------------------------------------- #
# Phase 5: warning feasibility
# --------------------------------------------------------------------------- #

def _with_allocators(model: OwnershipModel, names: set[str]) -> OwnershipModel:
    m = OwnershipModel()
    m.allocators = model.allocators | names
    m.deallocators = {k: set(v) for k, v in model.deallocators.items()}
    return m


@dataclass
class Feasibility:
    feasible: bool
    reason: str
    path_lines: list[int] = field(default_factory=list)


def leak_feasible(func: FunctionInfo, model: OwnershipModel, alloc_line: int | None = None,
                  alloc_callee: str | None = None) -> Feasibility:
    """Eq. (5): is there a feasible path on which the allocation is neither freed nor escaped?

    ``alloc_line``/``alloc_callee`` (from the warning) pick the allocation site; if it
    cannot be located, every allocation in the function is considered.
    """
    try:
        cfg = CFGBuilder.from_code(func.code, cpp=func.file.endswith((".cpp", ".cc", ".cxx", ".hpp")))
    except Exception as e:  # pragma: no cover
        return Feasibility(True, f"cfg failed ({e}); kept")
    al = Aliases(cfg)
    sites: list[tuple[int, Call]] = []
    for n, e in cfg.events():
        if isinstance(e, Call) and e.name in model.allocators:
            rel_line = func.start_line + n.line - 1
            if alloc_line is not None and abs(rel_line - alloc_line) > 1:
                continue
            if alloc_callee and e.name != alloc_callee:
                continue
            sites.append((n.id, e))
    if not sites and alloc_line is not None:
        # The analyzer asserts an allocation at this line (e.g. CodeQL followed a wrapper's
        # return chain that our summaries do not cover): trust it and track that call.
        for n, e in cfg.events():
            if isinstance(e, Call) and e.result and func.start_line + n.line - 1 == alloc_line \
                    and (alloc_callee is None or e.name == alloc_callee):
                sites.append((n.id, e))
        if sites:
            model = _with_allocators(model, {e.name for _, e in sites})
    if not sites:   # fall back to any allocation
        sites = [(n.id, e) for n, e in cfg.events() if isinstance(e, Call) and e.name in model.allocators]
    if not sites:
        return Feasibility(True, "no recognised allocation in function; kept for LLM")
    for nid, e in sites:
        if e.result is None or not is_plain_ident(e.result):
            # result discarded / stored straight into a field: analyzer's call, keep it
            return Feasibility(True, f"allocation at L{cfg.nodes[nid].line} not bound to a local; kept")
        C = al.cls(e.result)
        t = track(cfg, C, model, alloc_nodes={nid})
        pe, a, fr, es = encode(cfg, t)
        x = cfg.exit
        goal = z3.And(pe.reach[x], a[x], z3.Not(fr[x]), z3.Not(es[x]))
        path = pe.model_path(goal)
        if path is not None:
            lines = [cfg.nodes[i].line for i in path if cfg.nodes[i].line]
            return Feasibility(True, f"{e.name}() at L{cfg.nodes[nid].line} -> {sorted(C)} reaches exit unfreed", lines)
    return Feasibility(False, "every feasible path frees or transfers the allocation")
