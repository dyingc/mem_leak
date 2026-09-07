from pathlib import Path

from memhint.extract import Codebase, extract_file

SRC = b"""
typedef struct foo *foo_p;
typedef char *sds;
typedef void (*cb_t)(int);
#define XFREE(p) do { free(p); (p)=NULL; } while (0)
#define NOCALL 42
#define CALLS_OBJ free(global_buf)
static char *dup_it(const char *s) { char *r = malloc(strlen(s)+1); if (!r) return NULL; strcpy(r, s); return r; }
void foo_free(foo_p f) { if (f) { XFREE(f->name); free(f); } }
int plain(int a, long b) { return a + b; }
int uses_alias(sds s) { return s[0]; }
void takes_cb(cb_t cb) { cb(1); }
int main(void) { return 0; }
void run_test_case(char *x) { }
"""


def test_extract_snippet(tmp_path: Path):
    p = tmp_path / "a.c"
    p.write_bytes(SRC)
    funcs, aliases = extract_file(p, "a.c")
    by = {f.name: f for f in funcs}

    assert aliases == {"foo_p", "sds"}  # cb_t is a function pointer, not a heap-pointer alias
    assert set(by) == {"XFREE", "CALLS_OBJ", "dup_it", "foo_free", "plain", "uses_alias",
                       "takes_cb", "main", "run_test_case"}

    d = by["dup_it"]
    assert d.return_type == "char *" and d.is_static
    assert [(x.name, x.type) for x in d.params] == [("s", "const char *")]
    assert d.callees == {"malloc", "strlen", "strcpy"}

    f = by["foo_free"]
    assert f.params[0].type == "foo_p" and f.callees == {"XFREE", "free"}

    m = by["XFREE"]
    assert m.is_macro and [x.name for x in m.params] == ["p"] and m.callees == {"free"}
    assert by["CALLS_OBJ"].callees == {"free"}
    assert by["main"].params == []


def test_prefilter(tmp_path: Path):
    (tmp_path / "a.c").write_bytes(SRC)
    cb = Codebase.extract(tmp_path)
    names = {f.name for f in cb.candidates()}
    # pointer io or macro kept; main/test excluded; plain int function excluded
    assert names == {"XFREE", "CALLS_OBJ", "dup_it", "foo_free", "uses_alias"}  # cb_t is a fn-pointer typedef
    assert cb.functions["dup_it"].callers == set()
    assert cb.functions["XFREE"].callers == {"foo_free"}
