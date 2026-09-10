#!/usr/bin/env python3
"""Synthetic C corpus with one very large procedure body.

Pulse's fixpoint keeps an invariant map holding the pre- and post-state of *every CFG node*, and
each of those states is a list of up to --pulse-max-disjuncts disjuncts. The memory a single
procedure needs is therefore

    nodes  x  disjuncts  x  (size of one abstract state)

and it is all live at once, inside one procedure analysis, before any summary is built or stored.
--pulse-max-cfg-size (default 15000) allows procedures far larger than the point where this
product exceeds the address space. Depth of the call chain does not have the same effect: the
disjunct cap bounds each summary (measured: a 6-level chain peaks at 0.26 GB).

This generator emits one procedure of --stmts statements over distinct memory cells, with a branch
every --branch-every statements so the state keeps the maximum number of disjuncts alive, and a
few calls so the states are not trivial.
"""
import argparse, os

ap = argparse.ArgumentParser()
ap.add_argument('--stmts', type=int, default=6000, help='statements in the big procedure')
ap.add_argument('--branch-every', type=int, default=40, help='one two-way branch every N statements')
ap.add_argument('--cells', type=int, default=600, help='distinct struct fields / array slots')
ap.add_argument('--calls', type=int, default=8, help='calls to helper procedures inside the body')
ap.add_argument('--callee-cells', type=int, default=0, help='cells each called helper writes into its own sub-object; 0 keeps the helpers trivial')
ap.add_argument('--helper-levels', type=int, default=1, help='depth of the chain under each helper; deeper chains make every imported cell carry a longer trace')
ap.add_argument('--bigprocs', type=int, default=2, help='how many such procedures')
ap.add_argument('--units', type=int, default=2)
ap.add_argument('--out', required=True)
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
NF = max(1, a.cells // 2)
NA = max(8, a.cells - NF)

hdr = ['#include <stdlib.h>', '#include <string.h>', '',
       'struct wide {', '  ' + ' '.join('long f%d;' % i for i in range(NF)),
       '  int arr[%d];' % NA, '  struct wide *kid[%d];' % max(1, a.calls),
       '  struct wide *next; int tag;', '};', '']
for h in range(max(2, a.calls)):
    hdr.append('int helper%d(struct wide *w, int k);' % h)
for lv in range(1, a.helper_levels):
    for h in range(max(2, a.calls)):
        hdr.append('int sub%d_%d(struct wide *w, int k);' % (lv, h))
for i in range(a.bigprocs):
    hdr.append('int bigproc%d(struct wide *w, struct wide *v, int k);' % i)
open(os.path.join(a.out, 'wide.h'), 'w').write('\n'.join(hdr) + '\n')

def helper(h):
    S = ['int helper%d(struct wide *w, int k) {' % h, '  int acc = k;', '  if (!w) return 0;',
         '  if (k & 1) { acc += 2; } else { acc -= 2; }']
    # a helper with a large footprint: every call site that passes a distinct object adds this
    # many cells to the caller state, on top of what the caller body itself touches
    for c in range(a.callee_cells):
        if c % 2 == 0:
            S.append('  w->f%d = acc + %d;' % (c % NF, c))
        else:
            S.append('  w->arr[%d] = acc ^ %d;' % (c % NA, c))
    if a.helper_levels > 1:
        S.append('  if (w->kid[%d]) acc += sub1_%d(w->kid[%d], acc + 1);'
                 % (h % max(1, a.calls), h, h % max(1, a.calls)))
    S += ['  w->tag = acc;', '  return acc;', '}']
    return '\n'.join(S)


def sub(lv, h):
    """one link of the chain under a helper: writes its own cells, then goes one level deeper on a
    distinct sub-object, so the cells imported at the top carry a trace lv frames deep"""
    S = ['int sub%d_%d(struct wide *w, int k) {' % (lv, h), '  int acc = k;', '  if (!w) return 0;',
         '  if (k & 1) { acc += %d; } else { acc -= %d; }' % (lv + 1, lv + 1)]
    for c in range(a.callee_cells):
        if c % 2 == 0:
            S.append('  w->f%d = acc + %d;' % (c % NF, c + lv))
        else:
            S.append('  w->arr[%d] = acc ^ %d;' % (c % NA, c + lv))
    if lv + 1 < a.helper_levels:
        S.append('  if (w->kid[%d]) acc += sub%d_%d(w->kid[%d], acc + 1);'
                 % (h % max(1, a.calls), lv + 1, h, h % max(1, a.calls)))
    S += ['  w->tag = acc;', '  return acc;', '}']
    return '\n'.join(S)


helpers = [helper(h) for h in range(max(2, a.calls))]
for lv in range(1, a.helper_levels):
    helpers.extend(sub(lv, h) for h in range(max(2, a.calls)))

def bigproc(i):
    S = ['int bigproc%d(struct wide *w, struct wide *v, int k) {' % i,
         '  int acc = k; struct wide *p = w;', '  if (!w || !v) return 0;']
    depth = 0
    for n in range(a.stmts):
        if n % a.branch_every == 0 and n > 0:
            # a two-way branch: both sides survive as separate disjuncts
            S.append('  if ((acc >> %d) & 1) { p = w; acc += %d; } else { p = v; acc -= %d; }'
                     % (n % 30, n % 17 + 1, n % 13 + 1))
            depth += 1
        if n % 3 == 0:
            S.append('  p->f%d = acc + %d;' % (n % NF, n))
        elif n % 3 == 1:
            S.append('  p->arr[%d] = acc ^ %d;' % (n % NA, n))
        else:
            S.append('  acc += (int)p->f%d;' % ((n * 7) % NF))
        if a.calls and n % max(1, a.stmts // a.calls) == 0 and n > 0:
            j = (n // max(1, a.stmts // a.calls)) % a.calls
            # each call site gets its own sub-object so the helper footprints add up
            S.append('  if (w->kid[%d]) acc += helper%d(w->kid[%d], acc + %d);' % (j, j % max(2, a.calls), j, n))
    S += ['  return acc;', '}']
    return '\n'.join(S)

bodies = [[] for _ in range(a.units)]
bodies[0].extend(helpers)
for i in range(a.bigprocs):
    bodies[i % a.units].append(bigproc(i))
for u in range(a.units):
    open(os.path.join(a.out, 'w%d.c' % u), 'w').write('#include "wide.h"\n\n' + '\n\n'.join(bodies[u]) + '\n')
srcs = ' '.join('w%d.c' % u for u in range(a.units))
open(os.path.join(a.out, 'Makefile'), 'w').write(
    'CC ?= gcc\nCFLAGS ?= -O0 -g\nSRCS := %s\nOBJS := $(SRCS:.c=.o)\nall: $(OBJS)\n'
    '%%.o: %%.c wide.h\n\t$(CC) $(CFLAGS) -c $< -o $@\nclean:\n\trm -f $(OBJS)\n.PHONY: all clean\n' % srcs)
print('generated %d procedures of ~%d statements (%d branches each), %d distinct cells, '
      '%d calls to helpers writing %d cells each'
      % (a.bigprocs, a.stmts, a.stmts // a.branch_every, a.cells, a.calls, a.callee_cells))
