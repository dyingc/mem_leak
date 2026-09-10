#!/usr/bin/env python3
"""Synthetic C corpus that makes ONE procedure's Pulse analysis allocate a very large state.

Mechanism targeted (see notes/infer-pulse-oom-followup.md):
  state size  =  (number of distinct abstract cells touched)
               x (number of disjuncts, capped by --pulse-max-disjuncts)
               x (number of call sites that union those cells into the caller)
A leaf writes many distinct struct fields and array slots under nested branches, so its summary
has the maximum number of disjuncts, each with a large memory graph. Mid-level procedures call
several leaves on distinct objects, so the caller state is the union. Top-level procedures call
several mids. The blow-up therefore happens inside a single procedure analysis, before any
summary is stored -- which is the phase the current --pulse-max-heap check never observes.
"""
import argparse, os

ap = argparse.ArgumentParser()
ap.add_argument('--fields', type=int, default=64, help='struct fields written per leaf')
ap.add_argument('--arr', type=int, default=64, help='distinct array slots written per leaf')
ap.add_argument('--branches', type=int, default=5, help='nested branches per leaf (2^b paths)')
ap.add_argument('--leaves', type=int, default=4, help='distinct leaf procedures')
ap.add_argument('--fills', type=int, default=4, help='leaf calls per mid procedure')
ap.add_argument('--mids', type=int, default=4, help='mid calls per top procedure')
ap.add_argument('--tops', type=int, default=2, help='top procedures per unit')
ap.add_argument('--units', type=int, default=2)
ap.add_argument('--out', required=True)
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)

hdr = ['#include <stdlib.h>', '#include <string.h>', '',
       'struct big {', '  ' + ' '.join(f'long f{i};' for i in range(a.fields)),
       f'  int arr[{max(a.arr, 8)}];', '  struct big *next; struct big *prev; int tag;', '};', '']
for u in range(a.units):
    for i in range(a.leaves):
        hdr.append(f'int leaf{u}_{i}(struct big *b, int k);')
    for i in range(a.mids):
        hdr.append(f'int mid{u}_{i}(struct big *b, struct big *c, int k);')
    for i in range(a.tops):
        hdr.append(f'int top{u}_{i}(struct big *b, struct big *c, struct big *d, int k);')
open(os.path.join(a.out, 'big.h'), 'w').write('\n'.join(hdr) + '\n')

def leaf(u, i):
    L = [f'int leaf{u}_{i}(struct big *b, int k) {{', '  int acc = k;', '  if (!b) return 0;']
    ind = '  '
    # nested branches: every path writes a *different* value into every cell, so the disjuncts
    # stay distinct and cannot be collapsed by join_up_to
    for br in range(a.branches):
        L.append(f'{ind}if ((k >> {br}) & 1) {{')
        ind += '  '
        L.append(f'{ind}acc += {br + 1};')
    for br in range(a.branches):
        ind = ind[:-2]
        L.append(f'{ind}}} else {{ acc -= {br + 1}; }}')
    for f in range(a.fields):
        L.append(f'  b->f{f} = acc + {f * 7 + i};')
    for s in range(a.arr):
        L.append(f'  b->arr[{s % max(a.arr, 8)}] = acc ^ {s * 13 + i};')
    L += ['  b->tag = acc;', '  return acc;', '}']
    return '\n'.join(L)

def mid(u, i):
    L = [f'int mid{u}_{i}(struct big *b, struct big *c, int k) {{',
         '  int acc = k;', '  if (!b || !c) return 0;']
    for j in range(a.fills):
        tgt = 'b' if j % 2 == 0 else 'c'
        L.append(f'  acc += leaf{u}_{j % a.leaves}({tgt}, acc + {j});')
        if j % 2 == 1:
            L.append(f'  if (acc & {1 << (j % 5)}) {{ b->next = c; }} else {{ c->prev = b; }}')
    L += ['  return acc;', '}']
    return '\n'.join(L)

def top(u, i):
    L = [f'int top{u}_{i}(struct big *b, struct big *c, struct big *d, int k) {{',
         '  int acc = k;', '  if (!b || !c || !d) return 0;']
    for j in range(a.mids):
        pair = [('b', 'c'), ('c', 'd'), ('b', 'd'), ('d', 'b')][j % 4]
        L.append(f'  acc += mid{u}_{j % a.mids}({pair[0]}, {pair[1]}, acc + {j});')
    L += ['  return acc;', '}']
    return '\n'.join(L)

for u in range(a.units):
    src = ['#include "big.h"', '']
    src += [leaf(u, i) for i in range(a.leaves)]
    src += [mid(u, i) for i in range(a.mids)]
    src += [top(u, i) for i in range(a.tops)]
    open(os.path.join(a.out, f'b{u}.c'), 'w').write('\n\n'.join(src) + '\n')

srcs = ' '.join(f'b{u}.c' for u in range(a.units))
open(os.path.join(a.out, 'Makefile'), 'w').write(
    f'CC ?= gcc\nCFLAGS ?= -O0 -g\nSRCS := {srcs}\nOBJS := $(SRCS:.c=.o)\nall: $(OBJS)\n'
    f'%.o: %.c big.h\n\t$(CC) $(CFLAGS) -c $< -o $@\nclean:\n\trm -f $(OBJS)\n.PHONY: all clean\n')
print(f'generated {a.units} units, {a.leaves} leaves/{a.mids} mids/{a.tops} tops per unit '
      f'({a.fields} fields, {a.arr} array slots, 2^{a.branches} paths per leaf)')
