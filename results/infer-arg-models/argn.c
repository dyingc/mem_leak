#include <stdlib.h>
struct ctx;
extern void *my_alloc(int n);                     /* opaque allocator */
extern void my_free2(struct ctx *c, void *p);     /* opaque: frees 2nd argument */
extern void my_free3(struct ctx *c, int flags, void *p);  /* frees 3rd argument */
void leak_none(struct ctx *c) { void *p = my_alloc(4); (void)p; }
void ok_free2(struct ctx *c) { void *p = my_alloc(4); my_free2(c, p); }
void ok_free3(struct ctx *c) { void *p = my_alloc(4); my_free3(c, 0, p); }
void use_ctx_after(struct ctx *c) { void *p = my_alloc(4); my_free2(c, p); my_free2(c, my_alloc(2)); }
