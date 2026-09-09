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


# --------------------------------------------------------------------------- #
# out-parameter allocators (--pulse-model-alloc-arg-pattern)
# --------------------------------------------------------------------------- #

OUT_SRC = rb"""
struct R { int flags; };
struct C { struct R *cached; };

/* writes the fresh pointer straight through the out parameter */
int mk_direct(struct R **out) { *out = malloc(sizeof(struct R)); return 0; }

/* via a local, out parameter at index 1 */
int mk_indirect(int flags, struct R **out) {
    struct R *p = malloc(sizeof(struct R));
    if (p == NULL) return -1;
    p->flags = flags;
    *out = p;
    return 0;
}

/* out[0] = p is the same thing */
int mk_index(struct R **out) {
    struct R *p = malloc(sizeof(struct R));
    out[0] = p;
    return 0;
}

/* writes a borrowed pointer: not an allocator */
int borrow(struct C *c, struct R **out) { *out = c->cached; return 0; }

/* allocates but hands nothing over */
int internal(struct R **out) { struct R *p = malloc(sizeof(struct R)); free(p); return 0; }

/* two out parameters, only the first is filled with fresh memory */
int mk_two(struct R **a, struct C **b) {
    *a = malloc(sizeof(struct R));
    *b = 0;
    return 0;
}

void destroy(struct R *p) { free(p); }

/* callers */
void caller_leaks(int f) { struct R *p = 0; mk_indirect(f, &p); }
void caller_frees(int f) { struct R *p = 0; mk_indirect(f, &p); destroy(p); }
"""


def _out_cb(tmp_path):
    (tmp_path / "o.c").write_bytes(OUT_SRC)
    return Codebase.extract(tmp_path)


def test_out_param_allocator_validation(tmp_path):
    cb = _out_cb(tmp_path)
    A, D = Role.ALLOCATOR, Role.DEALLOCATOR
    cands = [Summary("mk_direct", A, "arg0"), Summary("mk_indirect", A, "arg1"),
             Summary("mk_index", A, "arg0"), Summary("borrow", A, "arg1"),
             Summary("internal", A, "arg0"), Summary("mk_two", A, "arg0"),
             Summary("mk_two", A, "arg1"), Summary("destroy", D, "arg0")]
    v = SummaryValidator(cb.functions, cands)
    ok = {(s.name, s.target) for s in v.validate_all(cands)}
    assert ("mk_direct", "arg0") in ok
    assert ("mk_indirect", "arg1") in ok
    assert ("mk_index", "arg0") in ok
    assert ("borrow", "arg1") not in ok        # borrowed pointer, no allocation
    assert ("internal", "arg0") not in ok      # freed before it could escape
    assert ("mk_two", "arg0") in ok            # the index selects the destination
    assert ("mk_two", "arg1") not in ok
    # the wrong index on a real allocator is still rejected
    assert not v.validate(Summary("mk_indirect", A, "arg0"))


def test_out_param_allocator_propagates_to_callers(tmp_path):
    cb = _out_cb(tmp_path)
    m = OwnershipModel()
    m.add(Summary("mk_indirect", Role.ALLOCATOR, "arg1"))
    m.add(Summary("destroy", Role.DEALLOCATOR, "arg0"))
    assert m.allocates_out("mk_indirect") == {1}
    assert "mk_indirect" not in m.allocators   # not a return-value allocator
    assert leak_feasible(cb.functions["caller_leaks"], m).feasible
    assert not leak_feasible(cb.functions["caller_frees"], m).feasible


def test_infer_patterns_split_by_target():
    from memhint.analyzers.infer import patterns, alloc_arg_patterns, free_arg_patterns
    A, D = Role.ALLOCATOR, Role.DEALLOCATOR
    S = [Summary("mk_ret", A, "return"), Summary("mk_out", A, "arg1"),
         Summary("fr0", D, "arg0"), Summary("fr2", D, "arg2")]
    ap, fp = patterns(S)
    assert ap == r"^\(mk_ret\)$"               # the out-param allocator is NOT modelled as a return
    assert fp == r"^\(fr0\)$"
    assert alloc_arg_patterns(S) == [r"1:^\(mk_out\)$"]
    assert free_arg_patterns(S) == [r"2:^\(fr2\)$"]
    # the reference implementation lumps everything together
    assert patterns(S, "official") == (r"mk_out\|mk_ret", r"fr0\|fr2")
