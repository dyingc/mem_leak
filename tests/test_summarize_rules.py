"""Project-rules injection into the Stage 1 summary prompt.

The point of the hook is that a project can describe its own memory architecture without any of
that vocabulary reaching this codebase, so the two things worth pinning down are that the default
changes nothing and that the rules land in exactly one place: after the generic instructions and
before the first function block, identically in every batch.
"""
from pathlib import Path

from memhint.extract import Codebase
from memhint.summarize import INSTRUCTIONS, build_prompt, rules_block

SRC = b"""
static char *dup_it(const char *s) { char *r = malloc(strlen(s)+1); if (!r) return NULL; strcpy(r, s); return r; }
void drop_it(char *p) { free(p); }
int plain(int a, long b) { return a + b; }
#define XFREE(p) do { free(p); (p)=NULL; } while (0)
"""

RULES = "Objects of type `thing_T` are reference counted; `thing_unref()` releases one reference."


def _cb(tmp_path: Path) -> Codebase:
    (tmp_path / "a.c").write_bytes(SRC)
    return Codebase.extract(tmp_path)


def test_default_is_byte_identical(tmp_path: Path):
    cb = _cb(tmp_path)
    batch = sorted(cb.functions.values(), key=lambda f: f.name)
    assert build_prompt(batch, cb) == build_prompt(batch, cb, "")
    assert build_prompt(batch, cb, "   \n  ") == build_prompt(batch, cb)
    assert rules_block("") == "" and rules_block(None) == ""


def test_rules_sit_between_instructions_and_functions(tmp_path: Path):
    cb = _cb(tmp_path)
    batch = sorted(cb.functions.values(), key=lambda f: f.name)
    p = build_prompt(batch, cb, RULES)

    assert p.startswith(INSTRUCTIONS)
    i_rules = p.index(RULES)
    i_first_fn = p.index("### Function 1:")
    assert len(INSTRUCTIONS) <= i_rules < i_first_fn

    # and the generic instructions are untouched by the injection
    assert build_prompt(batch, cb).startswith(INSTRUCTIONS)
    assert p[: len(INSTRUCTIONS)] == build_prompt(batch, cb)[: len(INSTRUCTIONS)]


def test_prefix_is_stable_across_batches(tmp_path: Path):
    """Every batch of a run must share a byte-identical prefix, or the provider cache never hits."""
    cb = _cb(tmp_path)
    fns = sorted(cb.functions.values(), key=lambda f: f.name)
    a = build_prompt(fns[:2], cb, RULES)
    b = build_prompt(fns[2:], cb, RULES)
    prefix = INSTRUCTIONS + rules_block(RULES)
    assert a.startswith(prefix) and b.startswith(prefix)
    assert a != b


def test_all_functions_mode(tmp_path: Path):
    cb = _cb(tmp_path)
    default = {f.name for f in cb.candidates()}
    every = {f.name for f in cb.candidates(all_functions=True)}

    assert "plain" not in default and "plain" in every      # no pointer in its signature
    assert "XFREE" in default and "XFREE" not in every      # macros are context, not targets
    assert "dup_it" in default and "dup_it" in every
