#!/usr/bin/env python3
"""Interpret an INFER_HEAP_TRACE JSONL file: last checkpoint before death, phase attribution,
and the interpretation rules from the follow-up request."""
import json, sys, collections
W = 8 / 2**30
rows = []
for line in open(sys.argv[1]):
    line = line.strip()
    if not line: continue
    try: rows.append(json.loads(line))
    except Exception: pass
if not rows: sys.exit('no records')
t0 = rows[0]['timestamp']
def g(r, k, d=0): return r.get(k, d)
print(f'records={len(rows)}  span={rows[-1]["timestamp"]-t0:.1f}s')
print(f'peak heap={max(g(r,"heap_words") for r in rows)*W:.2f}GiB  '
      f'peak rss={max(g(r,"rss_kib") for r in rows)/2**20:.2f}GiB  '
      f'peak vm={max(g(r,"vm_size_kib") for r in rows)/2**20:.2f}GiB')
print('\nphase counts:', dict(collections.Counter(r['phase'] for r in rows)))
print('\n-- last 12 checkpoints before the end of the trace:')
for r in rows[-12:]:
    print(f'  t={r["timestamp"]-t0:8.1f}s {r["phase"]:24s} heap={g(r,"heap_words")*W:6.2f}G '
          f'rss={g(r,"rss_kib")/2**20:6.2f}G vm={g(r,"vm_size_kib")/2**20:6.2f}G '
          f'cache={g(r,"summary_cache_entries")} {r.get("procedure","")[:40]} {r.get("instr","")[:60]}')
# unmatched proc-start (procedure that began and never ended): the failing phase
depth = []
for r in rows:
    if r['phase'] == 'proc-start': depth.append(r)
    elif r['phase'] in ('proc-end', 'proc-end-error', 'proc-exception'):
        if depth: depth.pop()
if depth:
    print(f'\n-- {len(depth)} procedure(s) started and never finished (innermost last):')
    for r in depth[-8:]:
        print(f'   {r.get("source_file","")}:{r.get("procedure","")} started at t={r["timestamp"]-t0:.1f}s '
              f'heap={g(r,"heap_words")*W:.2f}GiB rss={g(r,"rss_kib")/2**20:.2f}GiB')
# interpretation
last = rows[-1]
heap_g, rss_g, vm_g = g(last,'heap_words')*W, g(last,'rss_kib')/2**20, g(last,'vm_size_kib')/2**20
print('\n-- interpretation of the final state:')
print(f'   managed heap {heap_g:.2f}GiB vs RSS {rss_g:.2f}GiB vs VM {vm_g:.2f}GiB')
if rss_g > heap_g * 1.8:
    print('   RSS far above the managed heap: native allocation, serialization, database, or a compaction transient')
if vm_g > rss_g * 1.5:
    print('   virtual size far above RSS: address-space pressure (compaction reserves a second heap-sized chunk)')
ce = [g(r,'summary_cache_entries') for r in rows if g(r,'summary_cache_entries',-1) >= 0]
if ce: print(f'   summary cache entries: first={ce[0]} last={ce[-1]} max={max(ce)}')
pay = [(g(r,'serialized_payload_bytes'), r.get('procedure','')) for r in rows if g(r,'serialized_payload_bytes',-1) > 0]
if pay:
    pay.sort(reverse=True)
    print(f'   largest serialized payloads: ' + ', '.join(f'{b/2**20:.1f}MB {p[:30]}' for b, p in pay[:5]))
else:
    print('   no serialized payload above the 8 MB trace threshold')
