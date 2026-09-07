"""Core data structures shared across the MemHint pipeline.

A *function summary* (paper: Section III-A, Phase 2) is analyzer-agnostic:
``{name, role, target}`` where role is Allocator/Deallocator and target is
``return`` or ``argN``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


@dataclass
class Param:
    name: str
    type: str  # declared type text, e.g. "const char *"

    @property
    def is_pointer(self) -> bool:
        return "*" in self.type or "[" in self.type


@dataclass
class FunctionInfo:
    """One function (or function-like macro) extracted by Phase 1."""

    name: str
    return_type: str
    params: list[Param]
    code: str
    file: str
    start_line: int
    end_line: int
    callees: set[str] = field(default_factory=set)
    is_macro: bool = False
    is_static: bool = False
    # Filled in later by the call-graph step.
    callers: set[str] = field(default_factory=set)

    @property
    def signature(self) -> str:
        ps = ", ".join(f"{p.type} {p.name}".strip() for p in self.params)
        return f"{self.return_type} {self.name}({ps})"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["callees"] = sorted(self.callees)
        d["callers"] = sorted(self.callers)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FunctionInfo":
        d = dict(d)
        d["params"] = [Param(**p) for p in d.get("params", [])]
        d["callees"] = set(d.get("callees", []))
        d["callers"] = set(d.get("callers", []))
        return cls(**d)


class Role(str, Enum):
    ALLOCATOR = "Allocator"
    DEALLOCATOR = "Deallocator"


@dataclass(frozen=True)
class Summary:
    """Validated or candidate MM-function summary (paper Appendix B ``hints.json``)."""

    name: str
    role: Role
    target: str  # "return" | "argN"

    @property
    def arg_index(self) -> int:
        if self.target.startswith("arg"):
            return int(self.target[3:])
        return -1

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role.value, "target": self.target}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "Summary":
        return cls(name=d["name"], role=Role(d["role"]), target=d["target"])


@dataclass
class Warning:
    """One analyzer warning, normalised across CodeQL and Infer (Stage 3 input)."""

    analyzer: str  # "codeql" | "infer"
    rule: str
    file: str
    line: int
    function: str
    message: str
    allocation_site: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)  # [{file,line,message}]
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.analyzer}:{self.file}:{self.line}:{self.function}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Warning":
        return cls(**d)
