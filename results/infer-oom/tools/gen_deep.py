#!/usr/bin/env python3
"""Synthetic C corpus whose top procedure needs a very large Pulse state.

Pulse keeps, per disjunct, one abstract cell for every distinct memory location the procedure can
reach, together with the *trace* (call history) that reached it. Applying a callee summary
materialises the callee's footprint in the caller, rooted at the actual argument. The footprints
only add up if the call sites get *distinct* objects: passing the same pointer down the chain
makes every level write the same cells (measured: a 7-level chain over two shared objects peaks
at 0.9 GB). So each level here hands its callees distinct sub-objects, b->kid[j], and the top
procedure's state holds about F^(L-1) distinct objects of --cells cells each, every one carrying
a trace L frames deep. That product -- not any single allocation -- is what a large real
translation unit reproduces.

    cells per disjunct  ~=  --cells  x  --fanout ^ (--levels - 1)
    state size          ~=  cells x disjuncts (capped by --pulse-max-disjuncts)

The growth therefore happens inside the analysis of ONE procedure, before any summary is stored,
which is the phase Infer's per-instruction heap check and its summary-cache eviction never see.
"""
import argparse, os

ap = argparse.ArgumentParser()
ap.add_argument('--cells', type=int, default=400, help='distinct memory cells written by a leaf')
ap.add_argument('--branches', type=int, default=2, help='nested branches per leaf (2^b paths)')
ap.add_argument('--levels', type=int, default=5, help='call-chain depth, leaf included')
ap.add_argument('--fanout', type=int, default=4, help='calls per procedure at each level')
ap.add_argument('--units', type=int, default=2, help='translation units (levels are spread over them)')
ap.add_argument('--out', required=True)
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
F, L = a.fanout, a.levels
NFIELD = max(1, a.cells // 2)
NARR = max(8, a.cells - NFIELD)

def name(lvl, i):
    return 'leaf_%d' % i if lvl == 0 else 'l%d_%d' % (lvl, i)

# how many distinct procedures at each level: the leaf level is widest so that different call
# sites do not all share one summary
counts = [F] + [max(1, F // 2) for _ in range(1, L - 1)] + [1]

hdr = ['#include <stdlib.h>', '#include <string.h>', '',
       'struct big {', '  ' + ' '.join('long f%d;' % i for i in range(NFIELD)),
       '  int arr[%d];' % NARR, '  struct big *kid[%d];' % F,
       '  struct big *next; struct big *prev; int tag;', '};', '']
for lvl in range(L):
    for i in range(counts[lvl]):
        hdr.append('int %s(struct big *b, struct big *c, int k);' % name(lvl, i))
open(os.path.join(a.out, 'deep.h'), 'w').write('\n'.join(hdr) + '\n')

def leaf(i):
    S = ['int %s(struct big *b, struct big *c, int k) {' % name(0, i), '  int acc = k;',
         '  if (!b || !c) return 0;']
    ind = '  '
    for br in range(a.branches):
        S.append('%sif ((k >> %d) & 1) {' % (ind, br)); ind += '  '
        S.append('%sacc += %d;' % (ind, br + 1))
    for br in range(a.branches):
        ind = ind[:-2]
        S.append('%s} else { acc -= %d; }' % (ind, br + 1))
    # every write is to a distinct location with a distinct value, so the cells cannot be merged
    for f in range(NFIELD):
        S.append('  b->f%d = acc + %d;' % (f, f * 7 + i))
    for s in range(NARR):
        S.append('  c->arr[%d] = acc ^ %d;' % (s, s * 13 + i))
    S += ['  b->tag = acc; c->tag = acc + 1;', '  return acc;', '}']
    return '\n'.join(S)

def inner(lvl, i):
    S = ['int %s(struct big *b, struct big *c, int k) {' % name(lvl, i), '  int acc = k;',
         '  struct big *t;', '  if (!b || !c) return 0;']
    for j in range(F):
        callee = name(lvl - 1, j % counts[lvl - 1])
        # every call site gets its own pair of sub-objects, so the callee footprints add up
        # instead of aliasing onto the same cells
        S.append('  if (b->kid[%d] && c->kid[%d])' % (j, j))
        S.append('    acc += %s(b->kid[%d], c->kid[%d], acc + %d);' % (callee, j, j, j))
        if j % 2 == 1:
            S.append('  if (acc & %d) { t = b->next; b->next = c; c->prev = t; }' % (1 << (j % 5)))
    S += ['  return acc;', '}']
    return '\n'.join(S)

units = [[] for _ in range(a.units)]
for lvl in range(L):
    for i in range(counts[lvl]):
        body = leaf(i) if lvl == 0 else inner(lvl, i)
        units[lvl % a.units].append(body)
for u in range(a.units):
    open(os.path.join(a.out, 'd%d.c' % u), 'w').write(
        '#include "deep.h"\n\n' + '\n\n'.join(units[u]) + '\n')

srcs = ' '.join('d%d.c' % u for u in range(a.units))
open(os.path.join(a.out, 'Makefile'), 'w').write(
    'CC ?= gcc\nCFLAGS ?= -O0 -g\nSRCS := %s\nOBJS := $(SRCS:.c=.o)\nall: $(OBJS)\n'
    '%%.o: %%.c deep.h\n\t$(CC) $(CFLAGS) -c $< -o $@\nclean:\n\trm -f $(OBJS)\n.PHONY: all clean\n' % srcs)
est = a.cells * (F ** (L - 1))
print('generated %d units, levels=%d fanout=%d, %d procedures; '
      'about %d cells per disjunct at the top procedure' % (a.units, L, F, sum(counts), est))
