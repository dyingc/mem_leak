"""Phase 1 - Code extraction with Tree-sitter (paper Section III-A).

For every C/C++ source file we extract:
  * functions: name, return type, parameter list, body, direct callees
  * function-like macros, converted into the same record format
  * pointer typedef aliases (``typedef struct x *xp;``), used by the
    pointer-type pre-filter of Phase 2
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, Iterator

from tree_sitter import Language, Node, Parser
import tree_sitter_c
import tree_sitter_cpp

from .models import FunctionInfo, Param

log = logging.getLogger(__name__)

C_EXT = {".c"}
CPP_EXT = {".cpp", ".cc", ".cxx", ".hpp", ".hxx"}
HDR_EXT = {".h", ".hh"}
SOURCE_EXT = C_EXT | CPP_EXT | HDR_EXT

_C_LANG = Language(tree_sitter_c.language())
_CPP_LANG = Language(tree_sitter_cpp.language())

ENTRY_POINTS = {"main", "_main", "wmain"}


def parser_for(path: Path) -> Parser:
    return Parser(_CPP_LANG if path.suffix.lower() in CPP_EXT else _C_LANG)


def iter_nodes(root: Node) -> Iterator[Node]:
    """Pre-order traversal without recursion (Tree-sitter trees can be deep)."""
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _text(n: Node | None) -> str:
    return n.text.decode("utf-8", errors="ignore") if n is not None else ""


# --------------------------------------------------------------------------- #
# declarators
# --------------------------------------------------------------------------- #

def _unwrap_declarator(decl: Node | None) -> tuple[str, Node | None, str]:
    """Walk pointer/parenthesized/array wrappers around a declarator.

    Returns (identifier_name, function_declarator_or_None, pointer_prefix)
    where pointer_prefix is the '*'/'[]' decoration that belongs to the type.
    """
    prefix = ""
    fdecl = None
    while decl is not None:
        t = decl.type
        if t in ("pointer_declarator", "abstract_pointer_declarator"):
            prefix += "*"
            decl = decl.child_by_field_name("declarator")
        elif t in ("array_declarator", "abstract_array_declarator"):
            prefix += "[]"
            decl = decl.child_by_field_name("declarator")
        elif t in ("parenthesized_declarator", "abstract_parenthesized_declarator"):
            inner = [c for c in decl.children if c.is_named]
            decl = inner[0] if inner else None
        elif t in ("function_declarator", "abstract_function_declarator"):
            if fdecl is None:
                fdecl = decl
            decl = decl.child_by_field_name("declarator")
        elif t in ("identifier", "field_identifier", "type_identifier", "qualified_identifier",
                   "destructor_name", "operator_name", "template_function"):
            return _text(decl), fdecl, prefix
        elif t == "attributed_declarator":
            inner = [c for c in decl.children if c.is_named and "attribute" not in c.type]
            decl = inner[0] if inner else None
        else:
            return _text(decl), fdecl, prefix
    return "", fdecl, prefix


def _param_list(fdecl: Node) -> list[Param]:
    params: list[Param] = []
    plist = fdecl.child_by_field_name("parameters")
    if plist is None:
        return params
    for c in plist.children:
        if c.type == "parameter_declaration":
            ty = c.child_by_field_name("type")
            base = " ".join(_text(ch) for ch in c.children
                            if ch.is_named and ch != c.child_by_field_name("declarator"))
            name, inner_fdecl, prefix = _unwrap_declarator(c.child_by_field_name("declarator"))
            if inner_fdecl is not None:          # function pointer parameter
                prefix = "(*)" + prefix
            params.append(Param(name=name, type=(base + " " + prefix).strip() if prefix else base.strip()))
        elif c.type == "variadic_parameter":
            params.append(Param(name="...", type="..."))
        elif c.type == "identifier":              # K&R style: int f(a, b)
            params.append(Param(name=_text(c), type="int"))
    if len(params) == 1 and params[0].type == "void" and not params[0].name:
        return []
    return params


def _static(fn: Node) -> bool:
    return any(c.type == "storage_class_specifier" and _text(c) == "static" for c in fn.children)


def _callees(body: Node) -> set[str]:
    out: set[str] = set()
    for n in iter_nodes(body):
        if n.type == "call_expression":
            f = n.child_by_field_name("function")
            while f is not None and f.type == "parenthesized_expression":
                inner = [c for c in f.children if c.is_named]
                f = inner[0] if inner else None
            if f is None:
                continue
            if f.type in ("identifier", "qualified_identifier", "template_function"):
                out.add(_text(f))
            elif f.type == "field_expression":     # obj->fn(...) : keep the field name
                out.add(_text(f.child_by_field_name("field")))
    return out


# --------------------------------------------------------------------------- #
# per-file extraction
# --------------------------------------------------------------------------- #

def _extract_function(fn: Node, rel: str) -> FunctionInfo | None:
    decl = fn.child_by_field_name("declarator")
    name, fdecl, prefix = _unwrap_declarator(decl)
    if not name or fdecl is None:
        return None
    ret_base = _text(fn.child_by_field_name("type"))
    # qualifiers between storage class and type (e.g. "const")
    quals = [_text(c) for c in fn.children if c.type == "type_qualifier"]
    ret = " ".join(quals + [ret_base]) + (" " + prefix if prefix else "")
    body = fn.child_by_field_name("body")
    return FunctionInfo(
        name=name,
        return_type=ret.strip(),
        params=_param_list(fdecl),
        code=_text(fn),
        file=rel,
        start_line=fn.start_point[0] + 1,
        end_line=fn.end_point[0] + 1,
        callees=_callees(body) if body is not None else set(),
        is_static=_static(fn),
    )


_MACRO_CALL = re.compile(r"\b[A-Za-z_]\w*\s*\(")
_C_KEYWORDS = {"if", "while", "for", "switch", "return", "sizeof", "do", "else", "defined",
               "__attribute__", "__typeof__", "typeof", "_Alignof", "alignof", "case"}


def _extract_macro(m: Node, rel: str) -> FunctionInfo | None:
    name = _text(m.child_by_field_name("name"))
    value = _text(m.child_by_field_name("value"))
    if not name:
        return None
    params: list[Param] = []
    if m.type == "preproc_function_def":
        plist = m.child_by_field_name("parameters")
        params = [Param(name=_text(c), type="") for c in plist.children if c.type == "identifier"] if plist else []
    elif not _MACRO_CALL.search(value):
        return None  # object-like macro that does not expand to a call: not function-like
    callees = {mm.group(0)[:-1].strip() for mm in _MACRO_CALL.finditer(value)} - {name} - _C_KEYWORDS
    return FunctionInfo(
        name=name,
        return_type="",
        params=params,
        code=_text(m).rstrip(),
        file=rel,
        start_line=m.start_point[0] + 1,
        end_line=m.end_point[0] + 1,
        callees=callees,
        is_macro=True,
    )


def _pointer_typedefs(root: Node) -> set[str]:
    """Aliases whose typedef carries a '*' either on the declarator or on the base type."""
    out: set[str] = set()
    for n in iter_nodes(root):
        if n.type != "type_definition":
            continue
        base = _text(n.child_by_field_name("type"))
        for c in n.children:
            if c.type in ("type_identifier", "pointer_declarator", "array_declarator",
                          "parenthesized_declarator", "function_declarator"):
                name, fdecl, prefix = _unwrap_declarator(c)
                if fdecl is not None:
                    continue  # function-pointer typedef: not a heap pointer alias
                if name and ("*" in prefix or "*" in base):
                    out.add(name)
    return out


_COND_DIRECTIVE = re.compile(rb"^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|else|endif)\b.*$", re.M)


def _blank_conditionals(src: bytes) -> bytes:
    """Blank `#if/#else/#endif` lines (keeping line numbers) so that conditional
    compilation inside expressions/statements no longer breaks the parse."""
    return _COND_DIRECTIVE.sub(lambda m: b" " * 0, src)


def _collect(root: Node, rel: str) -> list[FunctionInfo]:
    funcs: list[FunctionInfo] = []
    for n in iter_nodes(root):
        if n.type == "function_definition":
            fi = _extract_function(n, rel)
            if fi:
                funcs.append(fi)
        elif n.type in ("preproc_function_def", "preproc_def"):
            fi = _extract_macro(n, rel)
            if fi:
                funcs.append(fi)
    return funcs


def extract_file(path: Path, rel: str) -> tuple[list[FunctionInfo], set[str]]:
    src = path.read_bytes()
    parser = parser_for(path)
    tree = parser.parse(src)
    funcs = _collect(tree.root_node, rel)
    aliases = _pointer_typedefs(tree.root_node)
    if tree.root_node.has_error:
        # Second pass on a copy with conditional directives blanked: recovers functions
        # such as Vim's expand_env_esc() whose bodies mix #ifdef into expressions.
        tree2 = parser.parse(_blank_conditionals(src))
        have = {(f.name, f.is_macro) for f in funcs}
        for f in _collect(tree2.root_node, rel):
            if (f.name, f.is_macro) not in have and not f.is_macro:
                funcs.append(f)
                have.add((f.name, f.is_macro))
        aliases |= _pointer_typedefs(tree2.root_node)
    return funcs, aliases


# --------------------------------------------------------------------------- #
# project level
# --------------------------------------------------------------------------- #

def iter_source_files(root: Path, exclude_dirs: Iterable[str] = (".git",)) -> Iterator[Path]:
    ex = set(exclude_dirs)
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in SOURCE_EXT and p.is_file() and not (set(p.parts) & ex):
            yield p


def _prefer(new: FunctionInfo, cur: FunctionInfo) -> bool:
    """Same name defined several times (platform #ifdefs, bundled tools).

    Prefer a definition in a .c/.cpp file over one in a header (e.g. Vim's
    ``alloc`` in alloc.c vs. the installer's copy in dosinst.h), then the
    longest body.
    """
    new_hdr = Path(new.file).suffix.lower() in HDR_EXT
    cur_hdr = Path(cur.file).suffix.lower() in HDR_EXT
    if new_hdr != cur_hdr:
        return cur_hdr
    return len(new.code) > len(cur.code)


class Codebase:
    """Result of Phase 1 over a whole project."""

    def __init__(self) -> None:
        self.functions: dict[str, FunctionInfo] = {}
        self.pointer_typedefs: set[str] = set()
        self.n_files = 0

    @classmethod
    def extract(cls, root: Path, source_root: Path | None = None) -> "Codebase":
        cb = cls()
        root = root.resolve()
        scan = (source_root or root).resolve()
        for p in iter_source_files(scan):
            rel = str(p.relative_to(root))
            try:
                funcs, aliases = extract_file(p, rel)
            except Exception as e:  # pragma: no cover - defensive
                log.warning("skip %s: %s", rel, e)
                continue
            cb.n_files += 1
            cb.pointer_typedefs |= aliases
            for f in funcs:
                cur = cb.functions.get(f.name)
                if cur is None or _prefer(f, cur):
                    cb.functions[f.name] = f
        cb._resolve_callers()
        log.info("extracted %d functions/macros from %d files (%d pointer typedefs)",
                 len(cb.functions), cb.n_files, len(cb.pointer_typedefs))
        return cb

    def _resolve_callers(self) -> None:
        for f in self.functions.values():
            for c in f.callees:
                if c in self.functions:
                    self.functions[c].callers.add(f.name)

    # ---- Phase 2 pre-filter (paper III-A) --------------------------------
    def has_pointer_io(self, f: FunctionInfo) -> bool:
        types = [f.return_type] + [p.type for p in f.params]
        for t in types:
            if "*" in t or "[" in t:
                return True
            tok = t.replace("const", "").replace("volatile", "").split()
            if tok and tok[-1] in self.pointer_typedefs:
                return True
        return False

    def candidates(self, all_functions: bool = False) -> list[FunctionInfo]:
        """Functions to ask the LLM about.

        Default: the pointer-signature pre-filter, plus function-like macros.
        ``all_functions``: every extracted non-macro function. Macros stay extracted either way --
        they are still pulled in as source context -- but this mode does not make them summary
        targets of their own.
        """
        out = []
        for f in self.functions.values():
            if f.name in ENTRY_POINTS or "test" in f.name.lower():
                continue
            if all_functions:
                if not f.is_macro:
                    out.append(f)
            elif f.is_macro or self.has_pointer_io(f):
                out.append(f)
        return out

    # ---- persistence -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "n_files": self.n_files,
            "pointer_typedefs": sorted(self.pointer_typedefs),
            "functions": [f.to_dict() for f in self.functions.values()],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Codebase":
        cb = cls()
        cb.n_files = d["n_files"]
        cb.pointer_typedefs = set(d["pointer_typedefs"])
        cb.functions = {f["name"]: FunctionInfo.from_dict(f) for f in d["functions"]}
        return cb
