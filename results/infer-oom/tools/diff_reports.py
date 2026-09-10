#!/usr/bin/env python3
"""Compare two Infer report.json files issue-by-issue (ignoring volatile fields)."""
import json, sys, collections
def key(i):
    return (i.get('bug_type'), i.get('file'), i.get('procedure'), i.get('line'), i.get('column'), i.get('qualifier'))
a = {key(i) for i in json.load(open(sys.argv[1]))}
b = {key(i) for i in json.load(open(sys.argv[2]))}
print(f'{sys.argv[1]}: {len(a)} issues')
print(f'{sys.argv[2]}: {len(b)} issues')
print(f'identical: {a == b}; only in first: {len(a - b)}; only in second: {len(b - a)}')
for k in sorted(a - b)[:10]: print('  - only in first :', k[0], k[1], k[2], k[3])
for k in sorted(b - a)[:10]: print('  + only in second:', k[0], k[1], k[2], k[3])
