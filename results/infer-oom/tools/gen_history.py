#!/usr/bin/env python3
"""Generate product-neutral C fixtures whose Pulse value history is a DAG.

Every shape ends in a null dereference, so Pulse reports an invalid access and
has to materialise the value history of the dereferenced pointer into an error
trace.  What differs between shapes is the *shape* of that history:

  doubling  `raw = raw + raw` repeated N times.  Each step builds
            BinaryOp(+, h, h) where both children are the *same* history node,
            so the stored history is a DAG with N nodes but 2**N root-to-leaf
            paths.  This is the exponential-traversal reproducer.
  linear    `raw = raw + t<i>` with a fresh t<i> each step: N nodes, N paths.
            The control: same instruction count, no sharing.
  fanout    N independent values combined pairwise into a balanced tree: the
            history is a genuine tree with N leaves and no sharing, so an
            honest traversal is O(N).

Usage: gen_history.py --shape doubling --n 24 --out doubling_null.c
"""
import argparse

ap = argparse.ArgumentParser()
ap.add_argument('--shape', choices=['doubling', 'linear', 'fanout'], default='doubling')
ap.add_argument('--n', type=int, default=24)
ap.add_argument('--out', required=True)
a = ap.parse_args()

L = ['#include <stdint.h>', '']
name = f'{a.shape}_null'
L.append(f'__attribute__((noinline)) int {name}(int x) {{')
# a value that is provably 0 but whose history records where it came from
L.append('  uintptr_t raw = (uintptr_t)0 + ((uintptr_t)x - (uintptr_t)x);')

if a.shape == 'doubling':
    for _ in range(a.n):
        L.append('  raw = raw + raw;')
elif a.shape == 'linear':
    for i in range(a.n):
        L.append(f'  uintptr_t t{i} = (uintptr_t)x - (uintptr_t)x;')
        L.append(f'  raw = raw + t{i};')
else:  # fanout
    for i in range(a.n):
        L.append(f'  uintptr_t v{i} = (uintptr_t)x - (uintptr_t)x;')
    cur = [f'v{i}' for i in range(a.n)]
    k = 0
    while len(cur) > 1:
        nxt = []
        for i in range(0, len(cur) - 1, 2):
            L.append(f'  uintptr_t s{k} = {cur[i]} + {cur[i + 1]};')
            nxt.append(f's{k}')
            k += 1
        if len(cur) % 2:
            nxt.append(cur[-1])
        cur = nxt
    L.append(f'  raw = raw + {cur[0]};')

L += ['  int *p = (int *)raw;', '  return *p;', '}', '']
open(a.out, 'w').write('\n'.join(L))
print(f'{a.out}: shape={a.shape} n={a.n} lines={len(L)}')
