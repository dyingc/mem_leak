#!/usr/bin/env python3
"""Synthetic C corpus in the shape macro-heavy code has after preprocessing.

A complex macro does not add source lines; it adds *branch points inside one statement*. After
preprocessing, one line becomes a tree of nested conditional operators (?:, &&, ||) whose operands
are re-evaluated (the classic macro pitfall), so for Pulse each such line is a dense sub-graph of
the CFG in which every node carries the accumulated path condition of the decisions taken to reach
it. That stresses two things the earlier fixtures did not:

  * branch density per statement -> the disjunct budget is spent and refilled at every line, and
    dropped disjuncts are recomputed at the next line;
  * the path condition itself -> every surviving disjunct carries a conjunction that grows with the
    number of decisions, so the arithmetic part of the state grows even when the heap graph does not.

Knobs: --depth is how deeply the macro nests conditionals, --uses how many times the macro appears
in one statement, --stmts how many such statements the function has. Keep an eye on the resulting
CFG size: above --pulse-max-cfg-size (default 15000) Pulse skips the procedure silently.
"""
import argparse, os

ap = argparse.ArgumentParser()
ap.add_argument('--depth', type=int, default=4, help='nesting depth of conditionals in the macro')
ap.add_argument('--uses', type=int, default=4, help='macro uses per statement')
ap.add_argument('--stmts', type=int, default=120, help='statements per procedure')
ap.add_argument('--cells', type=int, default=400, help='distinct struct fields available')
ap.add_argument('--procs', type=int, default=2, help='procedures per unit')
ap.add_argument('--units', type=int, default=2)
ap.add_argument('--out', required=True)
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
NF = max(4, a.cells // 2)
NA = max(8, a.cells - NF)

# The macro: every level re-evaluates its pointer argument (as a real macro does), tests a
# different field, and writes a different field on each side of the branch.
def macro_body(level, maxlevel):
    if level == maxlevel:
        return '((p) ? ((p)->f%%d = (k) + %d, (int)(p)->f%%d) : -%d)' % (level, level)
    inner_t = macro_body(level + 1, maxlevel)
    inner_f = macro_body(level + 1, maxlevel)
    return ('(((p) && (p)->arr[(i + %d) %% %d] > (k)) ? (%s) : ((p) ? ((p)->f%%d = (k) - %d, %s) : -%d))'
            % (level, NA, inner_t, level, inner_f, level))


raw = macro_body(0, a.depth)
n_holes = raw.count('%d')
# give each hole a distinct field so the expansion touches many cells
holes = ' , '.join('' for _ in range(0))
expanded = raw
for h in range(n_holes):
    expanded = expanded.replace('%d', str((h * 7) % NF), 1)

hdr = ['#include <stdlib.h>', '#include <string.h>', '',
       'struct mac {', '  ' + ' '.join('long f%d;' % i for i in range(NF)),
       '  int arr[%d];' % NA, '  struct mac *next; int tag;', '};', '',
       '/* one "macro" whose expansion is a tree of %d nested conditionals, each re-evaluating its'
       % (2 ** a.depth), '   pointer argument, exactly as a real complex macro does after preprocessing */',
       '#define STEP(p, i, k) %s' % expanded, '']
for u in range(a.units):
    for i in range(a.procs):
        hdr.append('int macro%d_%d(struct mac *p, int k);' % (u, i))
open(os.path.join(a.out, 'mac.h'), 'w').write('\n'.join(hdr) + '\n')

def proc(u, i):
    S = ['int macro%d_%d(struct mac *p, int k) {' % (u, i), '  int acc = k;', '  if (!p) return 0;']
    for n in range(a.stmts):
        uses = ' + '.join('STEP(p, %d, acc + %d)' % ((n * a.uses + j) % NA, j) for j in range(a.uses))
        S.append('  acc += %s;' % uses)
        if n % 8 == 7:
            S.append('  if (acc > %d) { p = p->next; if (!p) return acc; }' % (n * 13))
    S += ['  p->tag = acc;', '  return acc;', '}']
    return '\n'.join(S)

for u in range(a.units):
    body = '\n\n'.join(proc(u, i) for i in range(a.procs))
    open(os.path.join(a.out, 'm%d.c' % u), 'w').write('#include "mac.h"\n\n' + body + '\n')
srcs = ' '.join('m%d.c' % u for u in range(a.units))
open(os.path.join(a.out, 'Makefile'), 'w').write(
    'CC ?= gcc\nCFLAGS ?= -O0 -g\nSRCS := %s\nOBJS := $(SRCS:.c=.o)\nall: $(OBJS)\n'
    '%%.o: %%.c mac.h\n\t$(CC) $(CFLAGS) -c $< -o $@\nclean:\n\trm -f $(OBJS)\n.PHONY: all clean\n' % srcs)
print('macro expands to %d nested conditionals; %d uses x %d statements = about %d branch points '
      'per procedure' % (2 ** a.depth, a.uses, a.stmts, 2 ** a.depth * a.uses * a.stmts))
