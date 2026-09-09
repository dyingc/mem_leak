/* Generic C fixtures for --pulse-model-alloc-arg-pattern / --pulse-model-free-arg-pattern.
   Nothing here is specific to any library: the models are attached purely by the configured
   name patterns, never by the shape of the types. */
#include <stdlib.h>

struct R;
struct S;

/* ---- acquisition APIs -------------------------------------------------- */
extern void *make_resource(int n);            /* returns the resource         */
extern void make_a(struct R **out);           /* out parameter 0             */
extern int make_b(int flags, struct R **out); /* out parameter 1             */
extern int make_c(struct R **out, int flags); /* out parameter 0             */
extern void make_two(struct R **first, struct S **second); /* two out params */
extern int make_maybe(int ok, struct R **out); /* writes *out only on success */

/* ---- release APIs ------------------------------------------------------ */
extern void destroy_resource(struct R *p);            /* releases arg 0 */
extern void destroy_ctx1(void *ctx, struct R *p);     /* releases arg 1 */
extern void destroy_ctx2(void *a, void *b, struct R *p); /* releases arg 2 */
extern void destroy_s(struct S *p);

/* ---- decoys ------------------------------------------------------------ */
extern void ordinary_function(int flags, void *p);
extern void fill_int_out(int flags, int *out);   /* not a T** */
extern void borrow_ref(struct R **slot);         /* T** that does NOT acquire */

/* === Case A: return-value acquisition still works ======================= */
void leak_return(void) {
  void *p = make_resource(4);
  (void)p;
}

void no_leak_return(void) {
  struct R *p = (struct R *)make_resource(4);
  destroy_resource(p);
}

/* === Case B: out-parameter acquisition + leak =========================== */
void leak_out0(void) {
  struct R *p = 0;
  make_a(&p);
}

void leak_out1(void) {
  struct R *p = 0;
  make_b(7, &p);
}

void leak_out0_of_two_args(void) {
  struct R *p = 0;
  make_c(&p, 7);
}

/* === Case C: out-parameter acquisition + cleanup ======================== */
void no_leak_out0(void) {
  struct R *p = 0;
  make_a(&p);
  destroy_resource(p);
}

void no_leak_out1(void) {
  struct R *p = 0;
  make_b(7, &p);
  destroy_resource(p);
}

/* === Case D: wrong argument index must not acquire ====================== */
void no_acquire_wrong_index(void) {
  int x = 0;
  ordinary_function(3, &x);
}

/* argument 1 of this one is an int*, not a T**: the shape check must reject it */
void no_acquire_wrong_shape(void) {
  int x = 0;
  fill_int_out(3, &x);
}

/* === Case E: multiple out parameters, index selects the destination ===== */
/* only argument 0 is configured to acquire, so *second is untouched */
void leak_only_first_of_two(void) {
  struct R *a = 0;
  struct S *b = 0;
  make_two(&a, &b);
  destroy_s(b); /* b was never acquired; only a leaks */
}

void no_leak_first_of_two(void) {
  struct R *a = 0;
  struct S *b = 0;
  make_two(&a, &b);
  destroy_resource(a);
}

/* === Case F: conditional write (documented limitation) ================== */
void leak_conditional(int ok) {
  struct R *p = 0;
  if (make_maybe(ok, &p) == 0)
    destroy_resource(p);
  /* on the failure path nothing was really acquired, but the model is
     unconditional, so Pulse still sees a leak here */
}

/* === Case G: unrelated T** argument, not matched, no acquisition ======== */
void no_acquire_unmatched(void) {
  struct R *p = 0;
  borrow_ref(&p);
}

/* === regression: free at argument positions 0 / 1 / 2 =================== */
void no_leak_free_arg0(void) {
  struct R *p = 0;
  make_a(&p);
  destroy_resource(p);
}

void no_leak_free_arg1(void) {
  struct R *p = 0;
  make_a(&p);
  destroy_ctx1((void *)0, p);
}

void no_leak_free_arg2(void) {
  struct R *p = 0;
  make_a(&p);
  destroy_ctx2((void *)0, (void *)0, p);
}

/* === Case D': the selected argument is a plain pointer (void *), not T** === */
void no_acquire_plain_pointer(void) {
  int x = 0;
  ordinary_function(3, &x); /* arg 1 is void *, pointee is not a pointer */
}
