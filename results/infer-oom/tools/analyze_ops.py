#!/usr/bin/env python3
"""Rank abstract operations by how much they grow the state, from an INFER_HEAP_TRACE_OPS run.

Answers "which primitive operation multiplies the abstract state": for each operation kind and each
source location it reports how often it ran, how much major heap it added in total and at worst,
and how it changed the number of disjuncts. Use it to bisect a state explosion at the operation
level instead of by deleting code."""
import json, sys, collections
W = 8 / 2**20  # words -> MiB
rows = []
for line in open(sys.argv[1]):
    line = line.strip()
    if not line: continue
    try: r = json.loads(line)
    except Exception: continue
    ph = r.get('phase')
    if ph in ('op', 'op-start', 'op-error'): rows.append(r)
if not rows: sys.exit('no operation records: rerun with INFER_HEAP_TRACE_OPS=1')
started = [r for r in rows if r.get('phase') == 'op-start']
errors = [r for r in rows if r.get('phase') == 'op-error']
done = [r for r in rows if r.get('phase') == 'op']
print(f'{len(rows)} records: {len(done)} completed, {len(started)} started, {len(errors)} failed')
if errors:
    print('\n-- operations that raised (the process survived these):')
    for r in errors[-5:]:
        print(f'   {r.get("procedure","-")}  {r.get("kind")}  {r.get("loc")}  {r.get("exn","")[:60]}')
# the operation in flight when the trace stops: the last op-start with no matching op after it
if started:
    last_start = started[-1]
    last_done_ts = done[-1]['timestamp'] if done else 0
    if last_start['timestamp'] > last_done_ts:
        print('\n-- IN FLIGHT WHEN THE TRACE STOPPED (no completion record followed):')
        print(f'   procedure : {last_start.get("procedure","-")}')
        print(f'   operation : {last_start.get("kind")}  at {last_start.get("loc")}  node {last_start.get("node")}')
        print(f'   heap then : {last_start.get("heap_before",0)*8/2**30:.2f} GiB')
        print(f'   instruction: {(last_start.get("detail") or "")[:120]}')
    else:
        print('\n-- the trace ends on a completed operation, so the process was not killed inside one')
rows = done
by_kind = collections.defaultdict(lambda: [0, 0, 0, 0])   # count, total growth, max growth, disjunct delta
by_loc = collections.defaultdict(lambda: [0, 0, 0, '', 0, 0])
for r in rows:
    g = r.get('heap_after', 0) - r.get('heap_before', 0)
    din, dout = r.get('disjuncts_in', 0), r.get('disjuncts_out', 0)
    k = by_kind[r.get('kind', '?')]
    k[0] += 1; k[1] += max(0, g); k[2] = max(k[2], g); k[3] += dout - din
    key = (r.get('kind', '?'), r.get('loc', '-'))
    l = by_loc[key]
    l[0] += 1; l[1] += max(0, g); l[2] = max(l[2], g)
    if g >= l[2]: l[3] = (r.get('detail') or '')[:90]
    l[4] = max(l[4], dout); l[5] += max(0, r.get('dropped', 0))
print('\n-- by operation kind: count / total growth / worst single / net disjunct change')
for k, v in sorted(by_kind.items(), key=lambda x: -x[1][1]):
    print(f'  {k:10s} {v[0]:8d}  {v[1]*W:10.1f} MiB  {v[2]*W:8.1f} MiB  {v[3]:+d}')
print('\n-- top source locations by total heap growth')
print(f'  {"total":>10s} {"worst":>9s} {"n":>6s} {"maxdisj":>7s} {"dropped":>7s}  kind  location  instruction')
for (k, loc), v in sorted(by_loc.items(), key=lambda x: -x[1][1])[:20]:
    print(f'  {v[1]*W:10.1f} {v[2]*W:9.1f} {v[0]:6d} {v[4]:7d} {v[5]:7d}  {k:8s} {loc[:44]:44s} {v[3]}')
print('\n-- operations that multiplied the disjuncts the most (out - in)')
mult = sorted(rows, key=lambda r: -(r.get('disjuncts_out', 0) - r.get('disjuncts_in', 0)))[:10]
for r in mult:
    d = r.get('disjuncts_out', 0) - r.get('disjuncts_in', 0)
    if d <= 0: break
    print(f'  +{d:3d} disjuncts ({r.get("disjuncts_in")}->{r.get("disjuncts_out")}), '
          f'{(r.get("heap_after",0)-r.get("heap_before",0))*W:8.1f} MiB  {r.get("kind")}  '
          f'{r.get("loc","-")[:40]}  {(r.get("detail") or "")[:70]}')
