#!/usr/bin/env python3
"""Summarize an INFER_HEAP_TRACE file: heap growth per procedure (self and inclusive),
per top-level procedure, per target. heap_words are OCaml major-heap words (8 bytes)."""
import sys, re, collections
W = 8 / 2**30  # words -> GiB
ev = []
for line in open(sys.argv[1]):
    m = re.match(r'(\S+) (\S+) nesting=(-?\d+) heap_words=(\d+) top_heap_words=(\d+) minor=(\d+) major=(\d+) compact=(\d+) file=(\S+) proc=(.*)', line)
    if m: ev.append((float(m[1]), m[2], int(m[3]), int(m[4]), int(m[5]), int(m[6]), int(m[7]), int(m[8]), m[9], m[10]))
if not ev: sys.exit('no events')
t0 = ev[0][0]
print(f'events={len(ev)} span={ev[-1][0]-t0:.0f}s final heap={ev[-1][3]*W:.2f}GiB top={max(e[4] for e in ev)*W:.2f}GiB compactions={ev[-1][7]}')
# inclusive growth per procedure (start..end), plus max top_heap increase inside
stack = []; incl = []; tl = []; selfg = collections.Counter(); cnt = collections.Counter()
for e in ev:
    t, kind, nest, heap, top, mi, ma, co, f, p = e
    if kind == 'start': stack.append([p, f, heap, top, t, 0])
    elif kind in ('end', 'end-error'):
        if stack and stack[-1][0] == p:
            _, f0, h0, top0, t1, child = stack.pop()
            g = heap - h0
            incl.append((g, top - top0, t - t1, p, f0, len(stack)))
            selfg[f0 + ':' + p] += g - child; cnt[f0 + ':' + p] += 1
            if stack: stack[-1][5] += g
    elif kind.startswith('toplevel-done'):
        tl.append((heap, top, t - t0, p, kind))
    elif kind.startswith('target-done'):
        print(f'{kind}: heap={heap*W:.2f}GiB top={top*W:.2f}GiB t={t-t0:.0f}s file={f}')
print('\n-- top procedures by inclusive heap growth (GiB): growth / top-heap increase / secs / nesting / proc')
for g, tg, dt, p, f, n in sorted(incl, reverse=True)[:25]:
    print(f'{g*W:7.3f} {tg*W:7.3f} {dt:7.1f}s n={n:<3} {f}:{p}')
print('\n-- top procedures by SELF heap growth (GiB, excluding callees): growth / count / proc')
for k, g in selfg.most_common(20): print(f'{g*W:7.3f} x{cnt[k]:<3} {k}')
print('\n-- top procedures by wall time: secs / growth / proc')
for g, tg, dt, p, f, n in sorted(incl, key=lambda x: -x[2])[:15]:
    print(f'{dt:7.1f}s {g*W:7.3f} n={n:<3} {f}:{p}')
if tl:
    print('\n-- heap after each top-level procedure (sampled every N):')
    step = max(1, len(tl)//25)
    for i in range(0, len(tl), step):
        h, top, dt, p, k = tl[i]; print(f'  #{i:<5} t={dt:6.0f}s heap={h*W:.2f}GiB top={top*W:.2f}GiB {k} {p}')
    h, top, dt, p, k = tl[-1]; print(f'  #{len(tl)-1:<5} t={dt:6.0f}s heap={h*W:.2f}GiB top={top*W:.2f}GiB {k} {p}')
