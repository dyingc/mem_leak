"""Intraprocedural control-flow graph built from a Tree-sitter parse (paper III-A/III-C).

The CFG is *acyclic* like the paper's: ``if``/``switch``/``goto``/``return``
are modelled, loops are unrolled once (body executed 0 or 1 times) and
backward ``goto``s are dropped. Every node carries a list of *events* that
happen when it executes, in evaluation order:

  Call(name, args, result)   a call, possibly with its result stored in ``result``
  Assign(lhs, rhs)           an assignment (lhs/rhs are normalised expression texts)
  Return(expr)               a return statement

Branch nodes have a *condition key* (normalised text, with polarity) so that
two branches on the same condition share one Boolean in the Z3 encoding.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

from tree_sitter import Node

from .extract import _C_LANG, _CPP_LANG, _text
from tree_sitter import Parser


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #

@dataclass
class Call:
    name: str
    args: list[str]
    result: str | None = None    # lvalue receiving the return value ('__ret__' for `return f()`)
    line: int = 0


@dataclass
class Assign:
    lhs: str
    rhs: str
    line: int = 0


@dataclass
class Return:
    expr: str | None
    line: int = 0


Event = Call | Assign | Return


@dataclass
class CFGNode:
    id: int
    kind: str                     # ENTRY EXIT STMT BRANCH JOIN
    events: list[Event] = field(default_factory=list)
    line: int = 0
    # BRANCH only
    cond_key: str | None = None   # normalised condition text
    cond_kind: str = "bool"       # "bool" | "switch"
    succ: list[tuple[int, str | None]] = field(default_factory=list)  # (node, label) label: T/F/case:<v>/None
    pred: list[int] = field(default_factory=list)


class CFG:
    def __init__(self) -> None:
        self.nodes: dict[int, CFGNode] = {}
        self.entry = self._new("ENTRY")
        self.exit = self._new("EXIT")
        self.params: list[str] = []     # parameter names, in order
        self.locals: set[str] = set()   # params + local variable names

    def _new(self, kind: str, line: int = 0) -> int:
        n = CFGNode(len(self.nodes), kind, line=line)
        self.nodes[n.id] = n
        return n.id

    def edge(self, u: int, v: int, label: str | None = None) -> None:
        if u == self.exit:
            return
        if (v, label) not in self.nodes[u].succ:
            self.nodes[u].succ.append((v, label))
            self.nodes[v].pred.append(u)

    def __iter__(self) -> Iterator[CFGNode]:
        return iter(self.nodes.values())

    def events(self) -> Iterator[tuple[CFGNode, Event]]:
        for n in self.nodes.values():
            for e in n.events:
                yield n, e

    def dump(self) -> str:
        out = []
        for n in self.nodes.values():
            ev = "; ".join(_fmt(e) for e in n.events)
            cond = f" [{n.cond_kind} {n.cond_key}]" if n.kind == "BRANCH" else ""
            succ = ", ".join(f"{v}{'' if l is None else ':' + l}" for v, l in n.succ)
            out.append(f"{n.id:3d} {n.kind:6s} L{n.line:<4d}{cond} {ev}  -> {succ}")
        return "\n".join(out)


def _fmt(e: Event) -> str:
    if isinstance(e, Call):
        return f"{e.result + ' = ' if e.result else ''}{e.name}({', '.join(e.args)})"
    if isinstance(e, Assign):
        return f"{e.lhs} = {e.rhs}"
    return f"return {e.expr or ''}"


# --------------------------------------------------------------------------- #
# expression helpers
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    return _WS.sub("", text)


def base_ident(expr: str) -> str:
    """`(*p)->f[3]` -> `p`; `&x` -> `x`; `x` -> `x`."""
    m = re.search(r"[A-Za-z_]\w*", expr)
    return m.group(0) if m else expr


def is_plain_ident(expr: str) -> bool:
    return re.fullmatch(r"[A-Za-z_]\w*", expr) is not None


def _strip_parens(n: Node) -> Node:
    while n.type == "parenthesized_expression":
        inner = [c for c in n.children if c.is_named]
        if not inner:
            break
        n = inner[0]
    return n


def cond_key(text: str) -> tuple[str, bool]:
    """Normalise a condition into (key, polarity) so `!p`, `p == NULL` map to (p, False)."""
    t = norm(text)
    while t.startswith("(") and t.endswith(")") and _balanced(t[1:-1]):
        t = t[1:-1]
    m = re.fullmatch(r"(.+)==NULL|NULL==(.+)|(.+)==0", t)
    if m:
        inner = next(g for g in m.groups() if g)
        return cond_key(inner)[0], not cond_key(inner)[1]
    m = re.fullmatch(r"(.+)!=NULL|NULL!=(.+)|(.+)!=0", t)
    if m:
        inner = next(g for g in m.groups() if g)
        return cond_key(inner)
    if t.startswith("!") and not t.startswith("!="):
        k, pol = cond_key(t[1:])
        return k, not pol
    m = re.fullmatch(r"([A-Za-z_]\w*(?:->\w+|\.\w+)*)=[^=].*", t)   # (p = f()) -> p
    if m:
        return m.group(1), True
    return t, True


def _balanced(s: str) -> bool:
    d = 0
    for ch in s:
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
            if d < 0:
                return False
    return d == 0


# --------------------------------------------------------------------------- #
# builder
# --------------------------------------------------------------------------- #

class CFGBuilder:
    """Build a CFG for one function definition (or a parsed statement list)."""

    def __init__(self, source: bytes, cpp: bool = False):
        self.src = source
        self.cpp = cpp
        self.cfg = CFG()
        self.labels: dict[str, int] = {}
        self.pending_gotos: list[tuple[int, str]] = []
        self.loop_stack: list[tuple[int, int]] = []   # (continue_target, break_target)
        self.switch_stack: list[int] = []             # break target

    # ---- public ----------------------------------------------------------
    @classmethod
    def from_code(cls, code: str, cpp: bool = False) -> CFG:
        src = code.encode()
        tree = Parser(_CPP_LANG if cpp else _C_LANG).parse(src)
        fn = next((n for n in tree.root_node.children if n.type == "function_definition"), None)
        b = cls(src, cpp)
        if fn is None:
            # allow a bare statement list (tests, macro bodies)
            b._build_body(tree.root_node)
        else:
            from .extract import _unwrap_declarator, _param_list
            _, fdecl, _ = _unwrap_declarator(fn.child_by_field_name("declarator"))
            if fdecl is not None:
                b.cfg.params = [p.name for p in _param_list(fdecl)]
                b.cfg.locals.update(b.cfg.params)
            b._build_body(fn.child_by_field_name("body"))
        return b.cfg

    # ---- statements ------------------------------------------------------
    def _build_body(self, body: Node | None) -> None:
        cur = self.cfg.entry
        if body is not None:
            cur = self._stmt(body, cur)
        self.cfg.edge(cur, self.cfg.exit)          # falling off the end
        for src, label in self.pending_gotos:
            if label in self.labels:                # forward goto
                self.cfg.edge(src, self.labels[label])
        self._prune_unreachable()

    def _stmt(self, n: Node, cur: int) -> int:
        """Emit statement ``n`` starting from node ``cur``; return the node where flow continues."""
        t = n.type
        if t in ("compound_statement", "translation_unit"):
            for c in n.children:
                if c.is_named and c.type != "comment":
                    cur = self._stmt(c, cur)
            return cur
        if t in ("expression_statement", "declaration"):
            return self._simple(n, cur)
        if t == "return_statement":
            return self._return(n, cur)
        if t == "if_statement":
            return self._if(n, cur)
        if t == "switch_statement":
            return self._switch(n, cur)
        if t in ("while_statement", "for_statement", "do_statement"):
            return self._loop(n, cur)
        if t == "labeled_statement":
            lbl = _text(n.child_by_field_name("label"))
            j = self.cfg._new("JOIN", n.start_point[0] + 1)
            self.cfg.edge(cur, j)
            self.labels[lbl] = j
            inner = [c for c in n.children if c.is_named and c.type != "statement_identifier"]
            return self._stmt(inner[-1], j) if inner else j
        if t == "goto_statement":
            lbl = _text(n.child_by_field_name("label"))
            g = self.cfg._new("STMT", n.start_point[0] + 1)
            self.cfg.edge(cur, g)
            if lbl in self.labels:      # backward goto: dropped to stay acyclic
                return self._dead()
            self.pending_gotos.append((g, lbl))
            return self._dead()
        if t == "break_statement":
            target = self.switch_stack[-1] if self.switch_stack and (not self.loop_stack or
                     self._innermost_is_switch) else (self.loop_stack[-1][1] if self.loop_stack else None)
            if target is not None:
                self.cfg.edge(cur, target)
            return self._dead()
        if t == "continue_statement":
            if self.loop_stack:
                self.cfg.edge(cur, self.loop_stack[-1][0])
            return self._dead()
        if t in ("comment", "preproc_call", "preproc_def", "preproc_function_def", "preproc_include"):
            return cur
        if t in ("preproc_if", "preproc_ifdef", "preproc_elif", "preproc_else"):
            # treat #if blocks as an opaque branch: both arms possible
            return self._preproc_if(n, cur)
        if t in ("case_statement",):
            return self._stmt_list(n, cur)
        # try/catch, attributed statements, etc.: descend
        if any(c.is_named for c in n.children):
            for c in n.children:
                if c.is_named and c.type.endswith("statement") or c.type == "compound_statement":
                    cur = self._stmt(c, cur)
            return cur
        return cur

    def _stmt_list(self, n: Node, cur: int) -> int:
        for c in n.children:
            if c.is_named and (c.type.endswith("statement") or c.type == "declaration"):
                cur = self._stmt(c, cur)
        return cur

    def _dead(self) -> int:
        """A fresh node with no predecessor: code after return/goto/break."""
        return self.cfg._new("JOIN")

    @property
    def _innermost_is_switch(self) -> bool:
        return getattr(self, "_nest", [])[-1:] == ["switch"]

    def _push(self, kind: str) -> None:
        self.__dict__.setdefault("_nest", []).append(kind)

    def _pop(self) -> None:
        self.__dict__["_nest"].pop()

    def _simple(self, n: Node, cur: int) -> int:
        node = self.cfg._new("STMT", n.start_point[0] + 1)
        self.cfg.edge(cur, node)
        if n.type == "declaration":
            for c in n.children:
                if c.type == "init_declarator":
                    decl = c.child_by_field_name("declarator")
                    val = c.child_by_field_name("value")
                    name = base_ident(_text(decl))
                    self.cfg.locals.add(name)
                    if val is not None:
                        self._expr_events(val, node, assign_to=name)
                elif c.type.endswith("declarator") or c.type == "identifier":
                    self.cfg.locals.add(base_ident(_text(c)))
        else:
            for c in n.children:
                if c.is_named:
                    self._expr_events(c, node)
        return node

    def _return(self, n: Node, cur: int) -> int:
        node = self.cfg._new("STMT", n.start_point[0] + 1)
        self.cfg.edge(cur, node)
        exprs = [c for c in n.children if c.is_named]
        expr = None
        if exprs:
            e = _strip_parens(exprs[0])
            if e.type == "call_expression":
                self._expr_events(e, node, assign_to="__ret__")
                expr = "__ret__"
            else:
                self._expr_events(e, node)
                expr = norm(_text(e))
        nd = self.cfg.nodes[node]
        nd.events.append(Return(expr, nd.line))
        self.cfg.edge(node, self.cfg.exit)
        return self._dead()

    def _branch_node(self, cond: Node | None, cur: int, line: int) -> tuple[int, int]:
        """Evaluate condition side effects on a STMT node, then a BRANCH node. Returns (stmt, branch)."""
        pre = self.cfg._new("STMT", line)
        self.cfg.edge(cur, pre)
        if cond is not None:
            self._expr_events(cond, pre)
        br = self.cfg._new("BRANCH", line)
        self.cfg.edge(pre, br)
        return pre, br

    def _if(self, n: Node, cur: int) -> int:
        cond = n.child_by_field_name("condition")
        _, br = self._branch_node(cond, cur, n.start_point[0] + 1)
        key, pol = cond_key(_text(cond)) if cond is not None else ("?", True)
        b = self.cfg.nodes[br]
        b.cond_key = key
        join = self.cfg._new("JOIN")
        t_lbl, f_lbl = ("T", "F") if pol else ("F", "T")

        then_start = self.cfg._new("JOIN", n.start_point[0] + 1)
        self.cfg.edge(br, then_start, t_lbl)
        end_then = self._stmt(n.child_by_field_name("consequence"), then_start)
        self.cfg.edge(end_then, join)

        alt = n.child_by_field_name("alternative")
        else_start = self.cfg._new("JOIN")
        self.cfg.edge(br, else_start, f_lbl)
        if alt is not None:
            inner = [c for c in alt.children if c.is_named]
            end_else = self._stmt(inner[-1], else_start) if inner else else_start
        else:
            end_else = else_start
        self.cfg.edge(end_else, join)
        return join

    def _loop(self, n: Node, cur: int) -> int:
        line = n.start_point[0] + 1
        cond = n.child_by_field_name("condition")
        body = n.child_by_field_name("body")
        if n.type == "for_statement":
            init = n.child_by_field_name("initializer")
            if init is not None:
                cur = self._simple(init, cur) if init.type == "declaration" else self._simple_expr(init, cur)
        if n.type == "do_statement":
            # body runs at least once; then condition; no back edge
            join = self.cfg._new("JOIN")
            self._push("loop")
            self.loop_stack.append((join, join))
            end = self._stmt(body, cur)
            self.loop_stack.pop()
            self._pop()
            self.cfg.edge(end, join)
            if cond is not None:
                pre = self.cfg._new("STMT", line)
                self.cfg.edge(join, pre)
                self._expr_events(cond, pre)
                return pre
            return join
        _, br = self._branch_node(cond, cur, line)
        b = self.cfg.nodes[br]
        b.cond_key = cond_key(_text(cond))[0] if cond is not None else f"loop@{line}"
        pol = cond_key(_text(cond))[1] if cond is not None else True
        t_lbl, f_lbl = ("T", "F") if pol else ("F", "T")
        exit_join = self.cfg._new("JOIN")
        cont = self.cfg._new("JOIN")           # continue target (before update expr)
        body_start = self.cfg._new("JOIN", line)
        self.cfg.edge(br, body_start, t_lbl)
        self.cfg.edge(br, exit_join, f_lbl)
        self._push("loop")
        self.loop_stack.append((cont, exit_join))
        end = self._stmt(body, body_start)
        self.loop_stack.pop()
        self._pop()
        self.cfg.edge(end, cont)
        if n.type == "for_statement" and n.child_by_field_name("update") is not None:
            upd = self.cfg._new("STMT", line)
            self.cfg.edge(cont, upd)
            self._expr_events(n.child_by_field_name("update"), upd)
            self.cfg.edge(upd, exit_join)
        else:
            self.cfg.edge(cont, exit_join)
        return exit_join

    def _simple_expr(self, e: Node, cur: int) -> int:
        node = self.cfg._new("STMT", e.start_point[0] + 1)
        self.cfg.edge(cur, node)
        self._expr_events(e, node)
        return node

    def _switch(self, n: Node, cur: int) -> int:
        line = n.start_point[0] + 1
        cond = n.child_by_field_name("condition")
        _, br = self._branch_node(cond, cur, line)
        b = self.cfg.nodes[br]
        b.cond_key = cond_key(_text(cond))[0] if cond is not None else f"switch@{line}"
        b.cond_kind = "switch"
        exit_join = self.cfg._new("JOIN")
        self._push("switch")
        self.switch_stack.append(exit_join)
        body = n.child_by_field_name("body")
        prev_end: int | None = None
        has_default = False
        for c in (body.children if body is not None else []):
            if c.type != "case_statement":
                continue
            val = c.child_by_field_name("value")
            label = f"case:{norm(_text(val))}" if val is not None else "case:default"
            has_default |= val is None
            start = self.cfg._new("JOIN", c.start_point[0] + 1)
            self.cfg.edge(br, start, label)
            if prev_end is not None:                # fall-through
                self.cfg.edge(prev_end, start)
            prev_end = self._stmt_list(c, start)
        if prev_end is not None:
            self.cfg.edge(prev_end, exit_join)
        if not has_default:
            self.cfg.edge(br, exit_join, "case:default")
        self.switch_stack.pop()
        self._pop()
        return exit_join

    def _preproc_if(self, n: Node, cur: int) -> int:
        br = self.cfg._new("BRANCH", n.start_point[0] + 1)
        self.cfg.edge(cur, br)
        self.cfg.nodes[br].cond_key = f"#if@{n.start_point[0] + 1}:" + norm(_text(n.child_by_field_name("condition") or n.child_by_field_name("name") or n))[:40]
        join = self.cfg._new("JOIN")
        arm = self.cfg._new("JOIN")
        self.cfg.edge(br, arm, "T")
        end = arm
        alt = None
        for c in n.children:
            if c.type in ("preproc_else", "preproc_elif"):
                alt = c
                break
            if c.is_named and (c.type.endswith("statement") or c.type == "declaration"):
                end = self._stmt(c, end)
        self.cfg.edge(end, join)
        alt_start = self.cfg._new("JOIN")
        self.cfg.edge(br, alt_start, "F")
        end2 = self._stmt(alt, alt_start) if alt is not None else alt_start
        self.cfg.edge(end2, join)
        return join

    # ---- expressions -----------------------------------------------------
    def _expr_events(self, e: Node, node: CFGNode | int, assign_to: str | None = None) -> None:
        node = self.cfg.nodes[node] if isinstance(node, int) else node
        line = e.start_point[0] + 1
        e = _strip_parens(e)
        t = e.type
        if t == "assignment_expression":
            lhs = e.child_by_field_name("left")
            rhs = e.child_by_field_name("right")
            op = next((c for c in e.children if not c.is_named), None)
            lhs_t = norm(_text(lhs))
            r = _strip_parens(rhs)
            if r.type == "call_expression" and op is not None and _text(op) == "=":
                self._expr_events(r, node, assign_to=lhs_t)
            elif r.type == "assignment_expression" and _text(op) == "=":
                # a = b = f()
                self._expr_events(r, node)
                node.events.append(Assign(lhs_t, norm(_text(r.child_by_field_name("left"))), line))
            else:
                self._expr_events(rhs, node)
                node.events.append(Assign(lhs_t, norm(_text(rhs)) if _text(op) == "=" else lhs_t, line))
            if assign_to:
                node.events.append(Assign(assign_to, lhs_t, line))
            return
        if t == "call_expression":
            fn = _strip_parens(e.child_by_field_name("function"))
            args_n = e.child_by_field_name("arguments")
            args = [c for c in args_n.children if c.is_named and c.type != "comment"] if args_n else []
            for a in args:                               # nested calls first
                self._expr_events(a, node)
            name = _text(fn.child_by_field_name("field")) if fn.type == "field_expression" else _text(fn)
            node.events.append(Call(name, [norm(_text(a)) for a in args], assign_to, line))
            return
        if t in ("conditional_expression",):
            for c in e.children:
                if c.is_named:
                    self._expr_events(c, node)
            if assign_to:
                node.events.append(Assign(assign_to, norm(_text(e)), line))
            return
        if t == "cast_expression":
            self._expr_events(e.child_by_field_name("value"), node, assign_to)
            return
        if t in ("binary_expression", "unary_expression", "update_expression", "comma_expression",
                 "pointer_expression", "subscript_expression", "field_expression",
                 "compound_literal_expression", "sizeof_expression", "initializer_list"):
            for c in e.children:
                if c.is_named:
                    self._expr_events(c, node)
            if assign_to and t not in ("sizeof_expression",):
                node.events.append(Assign(assign_to, norm(_text(e)), line))
            return
        if assign_to:
            node.events.append(Assign(assign_to, norm(_text(e)), line))

    # ---- cleanup ---------------------------------------------------------
    def _prune_unreachable(self) -> None:
        seen = {self.cfg.entry}
        stack = [self.cfg.entry]
        while stack:
            u = stack.pop()
            for v, _ in self.cfg.nodes[u].succ:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        seen.add(self.cfg.exit)
        for nid in list(self.cfg.nodes):
            if nid not in seen:
                del self.cfg.nodes[nid]
        for n in self.cfg.nodes.values():
            n.succ = [(v, l) for v, l in n.succ if v in seen]
            n.pred = [u for u in n.pred if u in seen]
