/* Generic fixture for the INFER_ARGS / argfile transport test (see verify_transport.sh).
   Two translation units (transport_a.c, transport_b.c) so that `--jobs 2` really schedules
   analysis work in child worker processes, which only see the arguments infer forwards. */
#include <stdlib.h>
struct ctx;
extern void *my_alloc(int n);                  /* modelled allocator                          */
extern void *other_alloc(int n);               /* second alternative of the \| regex          */
extern void *my_alloc2(int n);                 /* DECOY: same prefix, must not match ^\(my_alloc\)$ */
extern void my_free2(struct ctx *c, void *p);  /* releases its 2nd argument (index 1)         */

void a_leak(void)             { void *p = my_alloc(4);    (void)p; }   /* reported            */
void a_leak_other(void)       { void *p = other_alloc(4); (void)p; }   /* reported via \|     */
void a_decoy(void)            { void *p = my_alloc2(4);   (void)p; }   /* never reported      */
void a_ok(struct ctx *c)      { void *p = my_alloc(4);    my_free2(c, p); }  /* clean with 1:^\(my_free2\)$ */
