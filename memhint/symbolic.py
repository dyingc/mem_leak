"""Z3 path encoding shared by Phase 3 (summary validation) and Phase 5 (warning filtering).

Paper equations:
  (3)  every node has at most one active incoming edge          AtMost1({e(u,n)})
       reach(n) <-> OR e(u,n),  reach(Entry) = True, e(u,n) -> reach(u)
  (4)  state(n) = OR (e(u,n) & state(u)) for ordinary nodes; set/cleared at special nodes
  (5)  leak feasible iff  SAT(reach(r) & alloc(r) & !freed(r) & !escaped(r))  for some exit r

Branch conditions are uninterpreted Booleans keyed by their normalised text,
so `if (x)` ... `if (!x)` on one path is UNSAT while everything else is only
constrained structurally (Section VI of the paper).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import z3

from .cfg import CFG


@dataclass
class StateSpec:
    """A Boolean state bit: set at ``set_nodes``, cleared at ``clear_nodes``, else inherited."""
    name: str
    set_nodes: set[int] = field(default_factory=set)
    clear_nodes: set[int] = field(default_factory=set)
    initial: bool = False


class PathEncoder:
    def __init__(self, cfg: CFG):
        self.cfg = cfg
        self.solver = z3.Solver()
        self.solver.set("timeout", 5000)
        self.edge: dict[tuple[int, int, str | None], z3.BoolRef] = {}
        self.reach: dict[int, z3.BoolRef] = {}
        self.cond: dict[str, z3.ExprRef] = {}
        self.states: dict[str, dict[int, z3.BoolRef]] = {}
        self.redefined = self._redefined_identifiers()
        self._encode_structure()

    def _redefined_identifiers(self) -> set[str]:
        """Identifiers with more than one definition in the function (parameters count
        as defined once at entry). Conditions mentioning them are *not* correlated
        across branch nodes, since the value may differ between the two tests."""
        from .cfg import Assign, Call, base_ident
        n: dict[str, int] = {p: 1 for p in self.cfg.params}
        for _, e in self.cfg.events():
            lhs = e.lhs if isinstance(e, Assign) else (e.result if isinstance(e, Call) else None)
            if lhs:
                b = base_ident(lhs)
                n[b] = n.get(b, 0) + 1
        return {k for k, v in n.items() if v >= 2}

    def _branch_key(self, node) -> str:
        key = node.cond_key or f"?@{node.id}"
        idents = set(re.findall(r"[A-Za-z_]\w*", key))
        if idents & self.redefined:
            return f"{key}@{node.id}"      # unique Boolean, paper-style b_v
        return key

    # ------------------------------------------------------------------ #
    _CONST = {"0": False, "FALSE": False, "false": False, "NULL": False,
              "1": True, "TRUE": True, "true": True}

    def _cond_var(self, key: str, kind: str) -> z3.ExprRef:
        k = f"{kind}:{key}"
        if k not in self.cond:
            if kind == "switch":
                self.cond[k] = z3.Int(f"s_{len(self.cond)}")
            elif key in self._CONST:
                self.cond[k] = z3.BoolVal(self._CONST[key])
            else:
                self.cond[k] = z3.Bool(f"b_{len(self.cond)}")
        return self.cond[k]

    def _encode_structure(self) -> None:
        s = self.solver
        cfg = self.cfg
        for n in cfg:
            self.reach[n.id] = z3.Bool(f"r_{n.id}")
        for n in cfg:
            for v, label in n.succ:
                self.edge[(n.id, v, label)] = z3.Bool(f"e_{n.id}_{v}")
        case_ids: dict[str, dict[str, int]] = {}
        for n in cfg:
            incoming = [self.edge[(u, n.id, l)] for u in n.pred for (v, l) in cfg.nodes[u].succ if v == n.id]
            if n.id == cfg.entry:
                s.add(self.reach[n.id])
            else:
                s.add(self.reach[n.id] == z3.Or(*incoming) if incoming else z3.Not(self.reach[n.id]))
                if len(incoming) > 1:
                    s.add(z3.AtMost(*incoming, 1))
            for v, label in n.succ:
                e = self.edge[(n.id, v, label)]
                s.add(z3.Implies(e, self.reach[n.id]))
                if n.kind == "BRANCH" and label is not None and n.cond_key is not None:
                    key = self._branch_key(n)
                    if n.cond_kind == "switch":
                        sv = self._cond_var(key, "switch")
                        ids = case_ids.setdefault(key, {})
                        cid = ids.setdefault(label, len(ids))
                        s.add(z3.Implies(e, sv == cid))
                    else:
                        b = self._cond_var(key, "bool")
                        s.add(z3.Implies(e, b if label == "T" else z3.Not(b)))

    # ------------------------------------------------------------------ #
    def add_state(self, spec: StateSpec) -> dict[int, z3.BoolRef]:
        s = self.solver
        st = {n.id: z3.Bool(f"{spec.name}_{n.id}") for n in self.cfg}
        for n in self.cfg:
            if n.id == self.cfg.entry:
                s.add(st[n.id] == spec.initial)
                continue
            inherited = z3.Or(*[z3.And(self.edge[(u, n.id, l)], st[u])
                                for u in n.pred for (v, l) in self.cfg.nodes[u].succ if v == n.id]) \
                if n.pred else z3.BoolVal(False)
            if n.id in spec.set_nodes:
                s.add(st[n.id] == z3.Or(inherited, self.reach[n.id]))
            elif n.id in spec.clear_nodes:
                s.add(st[n.id] == z3.BoolVal(False))
            else:
                s.add(st[n.id] == inherited)
        self.states[spec.name] = st
        return st

    def sat(self, *goal: z3.BoolRef) -> bool:
        self.solver.push()
        self.solver.add(*goal)
        r = self.solver.check()
        self.solver.pop()
        return r == z3.sat

    def model_path(self, *goal: z3.BoolRef) -> list[int] | None:
        """Return the node sequence of a satisfying path (for reports), or None."""
        self.solver.push()
        self.solver.add(*goal)
        if self.solver.check() != z3.sat:
            self.solver.pop()
            return None
        m = self.solver.model()
        self.solver.pop()
        active = {(u, v) for (u, v, l), e in self.edge.items() if z3.is_true(m.eval(e, model_completion=True))}
        path = [self.cfg.entry]
        seen = {self.cfg.entry}
        while True:
            nxt = [v for (u, v) in active if u == path[-1] and v not in seen]
            if not nxt:
                break
            path.append(nxt[0])
            seen.add(nxt[0])
        return path

    def exits(self) -> list[int]:
        return [n.id for n in self.cfg if n.id == self.cfg.exit]
