#!/usr/bin/env python3
"""Synthetic multi-TU C corpus generator to stress Infer/Pulse memory.

Design (all deterministic from --seed):
  * U translation units u<i>.c, each with P procedures p<i>_<j>.
  * Every procedure: struct/array locals, aliasing pointers, nested branches
    (depth B), loops with a function-pointer call, malloc/free on some paths,
    and K calls to callees chosen across units (cross-unit) plus a recursive
    ring chain r<i> -> r<i+1> ... -> r<0>.
  * A common header corpus.h declares everything so every TU sees all callees.
Knobs let us scale the fixture up/down while minimizing.
"""
import argparse, os, random, textwrap

ap = argparse.ArgumentParser()
ap.add_argument('--units', type=int, default=4)
ap.add_argument('--procs', type=int, default=20, help='procedures per unit')
ap.add_argument('--branches', type=int, default=4, help='nested if depth per proc')
ap.add_argument('--calls', type=int, default=6, help='callee calls per proc')
ap.add_argument('--loops', type=int, default=1, help='loops per proc')
ap.add_argument('--fields', type=int, default=6, help='struct fields')
ap.add_argument('--seed', type=int, default=1)
ap.add_argument('--out', required=True)
a = ap.parse_args()
rng = random.Random(a.seed)
os.makedirs(a.out, exist_ok=True)

names = [(i, j) for i in range(a.units) for j in range(a.procs)]
def pname(i, j): return f'p{i}_{j}'

hdr = ['#include <stdlib.h>', '#include <string.h>', '',
       'struct node { struct node *next; int *buf; int len; int tag; ',
       '  ' + ' '.join(f'long f{k};' for k in range(a.fields)) + ' };',
       'typedef int (*fp_t)(struct node *, int);',
       'extern int g_counter;', 'extern fp_t g_table[8];',
       'int helper_fp(struct node *n, int v);',
       'int helper_fp2(struct node *n, int v);']
for i, j in names:
    hdr.append(f'int {pname(i,j)}(struct node *n, int depth, int flag);')
for i in range(a.units):
    hdr.append(f'int r{i}(struct node *n, int depth);')
open(os.path.join(a.out, 'corpus.h'), 'w').write('\n'.join(hdr) + '\n')

def gen_proc(i, j):
    body = []
    body.append(f'int {pname(i,j)}(struct node *n, int depth, int flag) {{')
    body.append('  struct node local; struct node *alias = n ? n : &local; int arr[8]; int acc = flag;')
    body.append('  memset(&local, 0, sizeof local); local.len = 8; local.buf = arr;')
    body.append('  if (depth > 3) return acc;')
    # malloc on some paths
    if rng.random() < 0.6:
        body.append('  int *tmp = (int *)malloc(sizeof(int) * (flag > 0 ? flag : 1));')
        body.append('  if (!tmp) return -1;')
        body.append('  tmp[0] = depth; alias->buf = tmp;')
    else:
        body.append('  int *tmp = 0;')
    # nested branches
    ind = '  '
    for b in range(a.branches):
        f = rng.randrange(a.fields)
        cond = rng.choice([f'alias->f{f} > {rng.randrange(100)}', f'(acc & {1<<b}) != 0',
                           f'arr[{b % 8}] == depth', f'g_counter % {b+2} == {rng.randrange(b+2)}'])
        body.append(f'{ind}if ({cond}) {{')
        ind += '  '
        body.append(f'{ind}alias->f{f} += acc; arr[{b % 8}] = acc ^ depth; acc += {rng.randrange(1,9)};')
        if rng.random() < 0.5:
            body.append(f'{ind}alias = &local;')
        # else branch content emitted later by closing with else
    # closing with else parts
    for b in reversed(range(a.branches)):
        ind = ind[:-2]
        f = rng.randrange(a.fields)
        body.append(f'{ind}}} else {{ alias->f{f} -= acc; acc -= {rng.randrange(1,9)}; }}')
    # loops with fp calls
    for l in range(a.loops):
        body.append(f'  for (int k = 0; k < (flag & 7) + {l+1}; k++) {{')
        body.append(f'    fp_t fp = g_table[(acc + k) & 7]; if (fp) acc += fp(alias, k); else acc += helper_fp2(alias, k);')
        body.append(f'    if (acc > 1000) break;')
        body.append(f'    alias->f{rng.randrange(a.fields)} = acc;')
        body.append('  }')
    # cross-unit calls
    for c in range(a.calls):
        ci, cj = rng.choice(names)
        body.append(f'  acc += {pname(ci,cj)}(alias, depth + 1, acc & {rng.randrange(1,16)});')
        if rng.random() < 0.3:
            body.append(f'  if (acc < 0) {{ if (tmp) free(tmp); return acc; }}')
    body.append(f'  acc += r{i}(alias, depth + 1);')
    # free on most paths (leave some leaks on purpose)
    if rng.random() < 0.8:
        body.append('  if (tmp) { free(tmp); alias->buf = 0; }')
    body.append('  return acc;')
    body.append('}')
    return '\n'.join(body)

for i in range(a.units):
    src = ['#include "corpus.h"', '']
    if i == 0:
        src.append('int g_counter = 0;')
        src.append('int helper_fp(struct node *n, int v) { return n ? n->len + v : v; }')
        src.append('int helper_fp2(struct node *n, int v) { if (n && n->buf) return n->buf[v & 7]; return v; }')
        src.append('fp_t g_table[8] = { helper_fp, 0, helper_fp2, helper_fp, 0, 0, helper_fp2, 0 };')
    # recursive ring across units
    nxt = (i + 1) % a.units
    src.append(f'int r{i}(struct node *n, int depth) {{')
    src.append(f'  if (depth > 4 || !n) return depth;')
    src.append(f'  n->tag += depth; if (n->tag & 1) return r{nxt}(n, depth + 1) + {pname(i, 0)}(n, depth + 1, depth);')
    src.append(f'  return r{nxt}(n->next ? n->next : n, depth + 2);')
    src.append('}')
    for j in range(a.procs):
        src.append(gen_proc(i, j))
    open(os.path.join(a.out, f'u{i}.c'), 'w').write('\n\n'.join(src) + '\n')

mk = ['CC ?= gcc', 'CFLAGS ?= -O0 -g', 'SRCS := ' + ' '.join(f'u{i}.c' for i in range(a.units)),
      'OBJS := $(SRCS:.c=.o)', 'all: $(OBJS)', '%.o: %.c corpus.h', '\t$(CC) $(CFLAGS) -c $< -o $@',
      'clean:', '\trm -f $(OBJS)', '.PHONY: all clean']
open(os.path.join(a.out, 'Makefile'), 'w').write('\n'.join(mk) + '\n')
print(f'generated {a.units} units x {a.procs} procs in {a.out}')
