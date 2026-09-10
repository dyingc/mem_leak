#!/usr/bin/env python3
"""Generic fixture for the intraprocedural state explosion reported in
notes/infer-oom-structural-findings.md: ONE procedure of a few hundred lines whose statements are
uses of a macro that combines, in one expression,

  * a conditional lookup on a dynamic event/kind,
  * an indirect retrieval of a marshalled argument pointer,
  * a cast of that pointer to a control structure,
  * a nested field read through the cast pointer,
  * further conditional tests whose results are joined.

Each use makes Pulse case-split on several unknown pointers and materialise fresh abstract values
behind the cast, so the number of distinct alias/validity configurations per CFG node grows with
the number of uses while the source stays short. Whether that growth is bounded depends on
--pulse-max-disjuncts: the reported failure completes at 19 disjuncts and runs out of memory at
the default 20.
"""
import argparse, os

ap = argparse.ArgumentParser()
ap.add_argument('--uses', type=int, default=200, help='macro uses in the procedure')
ap.add_argument('--kinds', type=int, default=8, help='distinct event kinds tested')
ap.add_argument('--levels', type=int, default=3, help='nesting depth of the field read behind the cast')
ap.add_argument('--fields', type=int, default=64, help='distinct fields in the control structure')
ap.add_argument('--loop', action='store_true', help='wrap the uses in a loop over the event list, so widening interacts with the disjunct bound')
ap.add_argument('--writes', action='store_true', help='the macro also writes through the cast pointer, so disjuncts differ in heap shape and cannot be merged')
ap.add_argument('--procs', type=int, default=1)
ap.add_argument('--units', type=int, default=1)
ap.add_argument('--out', required=True)
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)

# nested field read behind the cast: ((struct ctrl *)e->arg)->inner->inner->…->f<n>
def nested(depth, field):
    p = '((struct ctrl *)(e)->arg)'
    for _ in range(depth):
        p = '(%s)->inner' % p
    if a.writes:
        # read-modify-write through the cast pointer: each use leaves a different heap shape in
        # each disjunct, so the join cannot collapse them
        return '(%s ? (int)((%s)->f%d = (%s)->f%d + (k)) : -3)' % (p, p, field, p, field)
    return '(%s ? (%s)->f%d : -3)' % (p, p, field)

hdr = ['#include <stdlib.h>', '#include <string.h>', '',
       'struct ctrl {', '  ' + ' '.join('long f%d;' % i for i in range(a.fields)),
       '  struct ctrl *inner; void *payload; int flags;', '};', '',
       'struct ev { int kind; void *arg; struct ctrl *ctl; struct ev *next; };', '',
       '/* one complex macro: conditional lookup, indirect argument retrieval, cast to a control',
       '   structure, nested field read, and a joined alternative branch */',
       '#define DYN(e, k, fld) \\',
       '  ( ((e) && (e)->kind == (k)) \\',
       '      ? ( ((e)->arg) \\',
       '            ? %s \\' % nested(a.levels, 0).replace('->f0', '->fld'),
       '            : -1 ) \\',
       '      : ( ((e) && (e)->ctl) ? (int)(e)->ctl->flags : -2 ) )', '']
for u in range(a.units):
    for i in range(a.procs):
        hdr.append('int dynproc%d_%d(struct ev *e, int k);' % (u, i))
open(os.path.join(a.out, 'dyn.h'), 'w').write('\n'.join(hdr) + '\n')

def proc(u, i):
    S = ['int dynproc%d_%d(struct ev *e, int k) {' % (u, i), '  int acc = k;', '  if (!e) return 0;']
    ind = '  '
    if a.loop:
        S.append('  while (e) {')
        ind = '    '
    for n in range(a.uses):
        S.append('%sacc += DYN(e, %d, f%d);' % (ind, n % a.kinds, (n * 7) % a.fields))
        # conditional tests on the accumulated value, so the case splits are joined and kept
        if n % 4 == 3:
            if a.loop:
                S.append('%sif (acc & %d) { acc -= %d; }' % (ind, 1 << (n % 16), n))
            else:
                S.append('%sif (acc & %d) { e = e->next; if (!e) return acc; }' % (ind, 1 << (n % 16)))
    if a.loop:
        S.append('    e = e->next;')
        S.append('  }')
    S += ['  return acc;', '}']
    return '\n'.join(S)

for u in range(a.units):
    body = '\n\n'.join(proc(u, i) for i in range(a.procs))
    open(os.path.join(a.out, 'y%d.c' % u), 'w').write('#include "dyn.h"\n\n' + body + '\n')
srcs = ' '.join('y%d.c' % u for u in range(a.units))
open(os.path.join(a.out, 'Makefile'), 'w').write(
    'CC ?= gcc\nCFLAGS ?= -O0 -g\nSRCS := %s\nOBJS := $(SRCS:.c=.o)\nall: $(OBJS)\n'
    '%%.o: %%.c dyn.h\n\t$(CC) $(CFLAGS) -c $< -o $@\nclean:\n\trm -f $(OBJS)\n.PHONY: all clean\n' % srcs)
print('%d procedure(s) of about %d source lines, %d macro uses, cast+%d-level nested field read'
      % (a.units * a.procs, a.uses + a.uses // 4 + 4, a.uses, a.levels))
