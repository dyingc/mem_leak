"""Paper Fig. 4 (a)-(d) summary validation and Fig. 5 leak feasibility."""
from memhint.analysis import SummaryValidator, OwnershipModel, leak_feasible
from memhint.extract import Codebase
from memhint.models import Summary, Role


SRC = rb"""
#define XFREE(p) do { free(p); (p) = NULL; } while (0)

struct obj { char *buf; int n; };

/* (a) valid allocator: alloc reaches return */
char *a_valid(int n) { char *p = malloc(n); if (!p) return NULL; return p; }

/* (b) rejected allocator: freed on all paths before return */
char *b_rejected(int n) { char *p = malloc(n); if (n > 3) { free(p); return NULL; } free(p); return NULL; }

/* (c) valid deallocator */
void c_valid(char *q) { if (q) free(q); }

/* (d) rejected deallocator: only a field is freed */
void d_rejected(struct obj *o) { free(o->buf); }

/* wrapper delegation: alias + transitive */
void wrap_free(void *x) { void *y = x; c_valid(y); }
char *wrap_alloc(int n) { return a_valid(n); }
char *wrap2(int n) { char *r = wrap_alloc(n); return r; }

/* infeasible: free only under a constant-false guard */
void never(char *p) { if (0) free(p); }

/* macro deallocator */
void via_macro(char *p) { XFREE(p); }

/* Fig. 5 */
int fig5(struct obj *o, int flag) {
    char *cert = a_valid(8);
    if (!cert) return 0;
    if (!flag) goto out_fail;
    o->buf = cert;
    return 1;
out_fail:
    return 0;
}
int fig5_fixed(struct obj *o, int flag) {
    char *cert = a_valid(8);
    if (!cert) return 0;
    if (!flag) goto out_fail;
    o->buf = cert;
    return 1;
out_fail:
    c_valid(cert);
    return 0;
}
int correlated(int x) {
    char *p = NULL;
    if (x) p = malloc(10);
    if (!x) return 0;
    free(p);
    return 1;
}
"""


def _cb(tmp_path):
    (tmp_path / "t.c").write_bytes(SRC)
    return Codebase.extract(tmp_path)


def test_fig4_and_delegation(tmp_path):
    cb = _cb(tmp_path)
    A, D = Role.ALLOCATOR, Role.DEALLOCATOR
    cands = [Summary("a_valid", A, "return"), Summary("b_rejected", A, "return"),
             Summary("c_valid", D, "arg0"), Summary("d_rejected", D, "arg0"),
             Summary("wrap_free", D, "arg0"), Summary("wrap_alloc", A, "return"), Summary("wrap2", A, "return"),
             Summary("never", D, "arg0"), Summary("via_macro", D, "arg0"), Summary("XFREE", D, "arg0")]
    v = SummaryValidator(cb.functions, cands)
    ok = {(s.name, s.role.value) for s in v.validate_all(cands)}
    assert ("a_valid", "Allocator") in ok
    assert ("b_rejected", "Allocator") not in ok
    assert ("c_valid", "Deallocator") in ok
    assert ("d_rejected", "Deallocator") not in ok
    assert ("wrap_free", "Deallocator") in ok
    assert ("wrap_alloc", "Allocator") in ok and ("wrap2", "Allocator") in ok
    assert ("never", "Deallocator") not in ok
    assert ("XFREE", "Deallocator") in ok and ("via_macro", "Deallocator") in ok


def test_fig5_feasibility(tmp_path):
    cb = _cb(tmp_path)
    m = OwnershipModel()
    m.add(Summary("a_valid", Role.ALLOCATOR, "return"))
    m.add(Summary("c_valid", Role.DEALLOCATOR, "arg0"))
    assert leak_feasible(cb.functions["fig5"], m).feasible
    assert not leak_feasible(cb.functions["fig5_fixed"], m).feasible
    assert not leak_feasible(cb.functions["correlated"], m).feasible
    # (b): freed on every path
    assert not leak_feasible(cb.functions["b_rejected"], m).feasible
    # (a): returned -> escaped
    assert not leak_feasible(cb.functions["a_valid"], m).feasible
