#!/usr/bin/env python3
"""Fixture for the global-initializer inlining path in Pulse.

infer/src/pulse/Pulse.ml, the Load case, calls set_global_astates: when the loaded expression is a
*global constant* (or a global function pointer under --pulse-inline-global-init-func-pointer),
Pulse inlines that global's initializer by dispatching a call to __infer_globals_initializer_<g>
-- and it does so at *every* load of that global. The upstream code carries the corresponding
"TODO: Initial global constants only once".

A dispatch/event table held in a global const array is therefore re-materialised into the abstract
state once per use. One Load instruction then grows the heap by as much as the whole table costs,
with one disjunct in and one disjunct out, which is exactly the shape reported for the stress case.

--entries scales the table, --uses how often the procedure reads it.
"""
import argparse, os

ap = argparse.ArgumentParser()
ap.add_argument('--entries', type=int, default=2000, help='rows in the global const table')
ap.add_argument('--uses', type=int, default=60, help='reads of the table in the procedure')
ap.add_argument('--fields', type=int, default=8, help='fields per row')
ap.add_argument('--const', dest='is_const', action='store_true', default=True)
ap.add_argument('--no-const', dest='is_const', action='store_false',
                help='drop the const qualifier: the initializer is then not inlined')
ap.add_argument('--out', required=True)
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)

qual = 'const ' if a.is_const else ''
rows = ',\n'.join('  { %d, %s }' % (i, ', '.join(str((i * 7 + f) % 251) for f in range(a.fields)))
                  for i in range(a.entries))
src = ['#include <stdlib.h>', '#include <string.h>', '',
       'struct row { int kind; %s };' % ' '.join('int v%d;' % f for f in range(a.fields)),
       '',
       '/* a dispatch/event table in a global %sarray: Pulse inlines its initializer at every load */'
       % ('const ' if a.is_const else 'non-const '),
       'static %sstruct row table[%d] = {' % (qual, a.entries), rows, '};', '',
       'struct ev { int kind; void *arg; struct ev *next; };', '',
       '/* the lookup, as a macro, with a conditional, an indirect argument and a nested read */',
       '#define LOOKUP(e, i) \\',
       '  ( ((e) && (e)->kind == (i)) \\',
       '      ? ( (e)->arg ? table[(i) %% %d].v0 + ((struct ev *)(e)->arg)->kind : table[(i) %% %d].v1 ) \\'
       % (a.entries, a.entries),
       '      : table[((i) + 1) %% %d].v2 )' % a.entries, '',
       'int lookup_proc(struct ev *e, int k) {', '  int acc = k;', '  if (!e) return 0;']
for n in range(a.uses):
    src.append('  acc += LOOKUP(e, %d);' % (n % max(1, a.entries)))
    if n % 8 == 7:
        src.append('  if (acc & %d) { e = e->next; if (!e) return acc; }' % (1 << (n % 16)))
src += ['  return acc;', '}']
open(os.path.join(a.out, 'g0.c'), 'w').write('\n'.join(src) + '\n')
open(os.path.join(a.out, 'Makefile'), 'w').write(
    'CC ?= gcc\nCFLAGS ?= -O0 -g\nall: g0.o\ng0.o: g0.c\n\t$(CC) $(CFLAGS) -c $< -o $@\n'
    'clean:\n\trm -f g0.o\n.PHONY: all clean\n')
print('global %stable of %d rows x %d fields, %d reads in one procedure'
      % ('const ' if a.is_const else '', a.entries, a.fields, a.uses))
